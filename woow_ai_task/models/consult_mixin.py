# -*- coding: utf-8 -*-
"""The AI form consultant (「詢問 AI 顧問」): staff press a button on a record
of another module and land in the internal AI chat of a consultation task
whose AI gets a capped summary of that record, read with the asker's own
rights. The mechanism is generic; each consulted model plugs in with a small
bridge module (woow_ai_task_crm for crm.lead):

* the consulted model inherits ``woow.project.ai.consult.mixin`` (with
  ``_name`` set) and implements its hooks;
* ``project.task._woow_project_ai_consult_link_specs()`` lists the task
  fields that link such records (LinkSpec: field, role, sequence, guard);
* one ``woow.project.ai.consult.profile`` per model (system administrators):
  off until enabled, its consultation project, a role prompt.

Who may use a task chat then also depends on the consulted records (the gate):
every record of the chat's ledger (``woow.project.ai.consult.sent``: what was
sent to the AI there) and every current link of an enabled model must be
readable by the person. One readability source per model,
``readable_query``: the record predicate (project.task
``_woow_project_ai_user_allowed``) executes it and the compiled channel rule
(discuss.channel ``_woow_project_ai_rule_of``) embeds it, so the two agree by
construction. Nothing of this costs anything until consultation is used (the
state, ``consult_state``: ormcached, cleared only by profile writes).

docs/PROJECT.md 「AI 顧問」 and 「穩定介面（橋接 API）」 describe the API bridges use.
"""

import logging
import re
from collections import namedtuple

import psycopg2

from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.tools import SQL
from odoo.tools.query import Query

from ..tools.ids import as_id, require_int_ids
from ..tools.record_text import MORE_NAMES, cap, clean_data_text, field_text
from .project_project import PROJECT_USER_GROUP, TAB_CUSTOMER, TAB_INTERNAL, TABS

_logger = logging.getLogger(__name__)

MIXIN = 'woow.project.ai.consult.mixin'
PROFILE = 'woow.project.ai.consult.profile'
LEDGER = 'woow.project.ai.consult.sent'

ROLE_PRIMARY = 'primary'
ROLE_EXTRA = 'extra'

# a task field linking consulted records: `field` a stored many2one or many2many of project.task to a mixin
# model; `role` primary or extra; `sequence` the order of the links in the AI's data; `guard` False only where
# the owning module keeps its own binding guard (PaaS)
LinkSpec = namedtuple('LinkSpec', 'field role sequence guard')

# the state of consultation in this database (consult_state): enabled models, whether consultation was ever
# used (a profile was ever enabled, or the ledger has a row: then the gate applies), ledger models that are not
# mixin models of this registry (a bridge was uninstalled), the projects of the profiles
State = namedtuple('State', 'enabled_models used ledger_other_models profile_project_ids')
UNUSED = State(frozenset(), False, frozenset(), frozenset())

# what a declared consult field may not be (check_field_rules)
FORBIDDEN_FIELD_TYPES = frozenset(('binary', 'image', 'json', 'properties', 'properties_definition', 'reference',
                                   'many2one_reference'))
FORBIDDEN_NAME_RE = re.compile(
    r'password|passwd|secret|token|api[_-]?key|credential|signup|email|phone|mobile|street|zip|vat|iban|bank',
    re.IGNORECASE)
MAX_RELATED_NAMES = 10
# characters of the record's name in a consultation task's name (N6 B)
MAX_TASK_NAME = 80
# continuing a consultation: the newest candidate tasks looked at
MAX_CONTINUE_CANDIDATES = 20

# (db, model, uid) whose records could not be checked, (db, model, field) refused: logged once per process
_query_errors_logged = set()
_field_errors_logged = set()


# --- Dispatch ---

def consult_models(env):
    """The (non-abstract) models of this registry that inherit the consultant
    mixin: the registry's own inheritance tree (``_inherit_children``, which
    Odoo keeps while it loads modules), always current, no query. The same
    models as ``issubclass(registry[name], registry[MIXIN])``."""
    registry = env.registry
    if MIXIN not in registry:
        return frozenset()
    return frozenset(name for name in registry.descendants([MIXIN], '_inherit')
                     if name in registry and not registry[name]._abstract)


def is_consult_model(env, name):
    """Whether `name` is a consulted model (it inherits the mixin)."""
    return bool(name) and name in consult_models(env)


def consult_state(env):
    """The consultation state (State) of the database, for this registry:
    ormcached, cleared by every profile create / write / unlink only."""
    if PROFILE not in env.registry:
        return UNUSED
    return env[PROFILE].sudo()._woow_project_ai_consult_state()


def model_tabs(env, name):
    """The tabs records of `name` are sent to (a mixin model's own hook)."""
    if not is_consult_model(env, name):
        return frozenset()
    return frozenset(tab for tab in env[name]._woow_project_ai_consult_tabs() if tab in TABS)


def consult_keys(env, state):
    """K: the (model, tab) pairs a ledger row may be checked against. Every
    mixin model of the registry with its tabs (a first ledger row of a model
    is always in K: no cache clear when rows are inserted), and every tab for
    a model of the ledger this registry has no mixin for any more."""
    keys = {(name, tab) for name in consult_models(env) for tab in model_tabs(env, name)}
    keys |= {(name, tab) for name in state.ledger_other_models for tab in TABS}
    return keys


# --- One readability source per model ---

def default_readable_query(model, user, tab, candidates=None):
    """Odoo's own rules as `user` (`model` is in that user's environment,
    over their active companies, archived records included): a Query, or
    None for an inactive or public user, a non-employee in the internal tab,
    or no read access to the model at all."""
    if not user or not user.active or user._is_public():
        return None
    if tab == TAB_INTERNAL and not user._is_internal():
        return None
    if not model.browse().has_access('read'):
        return None
    return model._search([('id', 'in', candidates)] if candidates is not None else [])


def readable_query(env, model_name, user, tab, companies, candidates=None):
    """The ids of `model_name` that `user` may read for the AI tab `tab`, as
    SQL usable after IN; None when nothing can be said (the model is not in
    this registry, or the check failed): every record of the model is then
    unreadable for this user (fails closed for that model only).

    `candidates` (ids or an SQL sub-select): only those are asked about;
    the hooks always get them as an SQL sub-select usable after IN (or None
    for all). A mixin model answers through its hook
    ``_woow_project_ai_consult_readable_query`` (called in the user's
    environment), any other model through Odoo's own rules. The rule embeds
    the answer as ``NOT COALESCE(x IN R, FALSE)`` and the predicate as
    ``x IN R``: a NULL in R cannot make a record readable either way."""
    if not model_name or model_name not in env.registry or not user:
        return None
    if candidates is not None and not isinstance(candidates, SQL):
        ids = sorted({value for value in (as_id(i) for i in candidates) if value})
        candidates = SQL("(SELECT unnest(%s::int[]))", ids)
    try:
        model = env[model_name].with_user(user).with_context(active_test=False,
                                                               allowed_company_ids=list(companies))
        if is_consult_model(env, model_name):
            result = model._woow_project_ai_consult_readable_query(user, tab, candidates)
        else:
            result = default_readable_query(model, user, tab, candidates)
        if result is None:
            return None
        if isinstance(result, Query):
            return result.subselect()
        if isinstance(result, SQL):
            return result
        raise TypeError('readable query of %s: %r' % (model_name, type(result)))
    except psycopg2.Error:
        raise  # the transaction is aborted: swallowing it would only move the failure to the next query
    except Exception:  # noqa: BLE001 - AccessError, a rule that cannot be evaluated for this user...
        key = (env.cr.dbname, model_name, user.id)
        if key not in _query_errors_logged:
            _query_errors_logged.add(key)
            _logger.warning("Project AI consultant: records of %s cannot be checked for user %s; none of them is "
                            "readable for them", model_name, user.id, exc_info=True)
        return None


def readable_ids(env, model_name, ids, user, tab, companies):
    """Those of `ids` (records of `model_name`) that `user` may read for the
    AI tab `tab`: one query. A deleted record is in no answer."""
    ids = sorted({value for value in (as_id(i) for i in ids) if value})
    if not ids:
        return set()
    query = readable_query(env, model_name, user, tab, companies, candidates=ids)
    if query is None:
        return set()
    rows = env.execute_query(SQL("SELECT x FROM unnest(%s::int[]) AS x WHERE x IN %s", ids, query))
    return {row[0] for row in rows}


def company_ids(user):
    """The companies `user` may work in (project.task._woow_project_ai_company_ids)."""
    return list(user.sudo()._get_company_ids())


def shared_with(records, audience):
    """Those of `records` that every user of `audience` can read too (an
    answer is read by every member of the chat)."""
    for member in audience or ():
        if not records:
            break
        readable = records.with_user(member).with_context(
            allowed_company_ids=company_ids(member))._filtered_access('read')
        records = records.browse(readable.ids)
    return records


# --- What a declared field may be ---

def field_rule_problem(model, name):
    """Why `name` may not be declared as a consult field of `model` (a short
    reason), or None. Relational fields must also point to a model that every
    user who can read the record can read: each bridge's tests check that
    (consult_common.assert_field_rules)."""
    field = model._fields.get(name)
    if field is None:
        return 'no such field'
    if field.groups:
        return 'restricted to groups'
    if field.type in FORBIDDEN_FIELD_TYPES:
        return 'field type %s' % field.type
    if FORBIDDEN_NAME_RE.search(name):
        return 'personal or secret data'
    if field.compute and not field.store and field.compute_sudo:
        # a related or computed value read as superuser: not the asker's own rights
        return 'computed as superuser'
    return None


def check_field_rules(model, names):
    """The names among `names` that may be sent; the others are skipped and
    logged once per process."""
    good = []
    for name in names or ():
        problem = field_rule_problem(model, name)
        if problem:
            key = (model.env.cr.dbname, model._name, name)
            if key not in _field_errors_logged:
                _field_errors_logged.add(key)
                _logger.warning("Project AI consultant: field %s.%s is never sent to the AI (%s)", model._name,
                                name, problem)
            continue
        good.append(name)
    return tuple(good)


class WoowProjectAiConsultMixin(models.AbstractModel):
    """A model whose records staff may ask the AI consultant about.

    A bridge module sets ``_name = '<model>'`` next to ``_inherit = ['<model>',
    'woow.project.ai.consult.mixin']`` (with two entries and no ``_name``,
    Odoo would name the model after the class), adds a link field on
    project.task with its LinkSpec, seeds a disabled profile, and implements
    at least ``_woow_project_ai_consult_task_values`` and
    ``_woow_project_ai_consult_task_domain``."""
    _name = MIXIN
    _description = 'AI Consultant Record'

    woow_project_ai_consult_enabled = fields.Boolean(
        string='Can Ask the AI Consultant', compute='_compute_woow_project_ai_consult_enabled',
        groups='base.group_user',
        help='The AI consultant is enabled for this kind of record and you have the user rights of the Project '
             'app.')
    woow_project_ai_consult_task_count = fields.Integer(
        string='AI Consultations', compute='_compute_woow_project_ai_consult_task_count', groups='base.group_user')

    @api.depends_context('uid')
    def _compute_woow_project_ai_consult_enabled(self):
        allowed = self._woow_project_ai_consult_allowed()
        for record in self:
            record.woow_project_ai_consult_enabled = allowed

    def _woow_project_ai_consult_allowed(self):
        """The model is enabled and the current user is an employee with the
        user rights of the Project app (whether they can read the
        consultation project is told when they press the button)."""
        user = self.env.user
        return self._name in consult_state(self.env).enabled_models and bool(user) and user._is_internal() \
            and user.has_group(PROJECT_USER_GROUP)

    @api.depends_context('uid')
    def _compute_woow_project_ai_consult_task_count(self):
        allowed = self._woow_project_ai_consult_allowed()
        for record in self:
            count = 0
            if allowed and as_id(record.id):
                try:
                    count = len(record._woow_project_ai_consult_readable_tasks())
                except AccessError:
                    count = 0
            record.woow_project_ai_consult_task_count = count

    def _woow_project_ai_consult_readable_tasks(self, extra_domain=None, limit=None):
        """The consultation tasks of this record the current user can read,
        newest first (sudo ids of the task domain: link fields may have
        groups, which a non-superuser search refuses; then the user's own
        search)."""
        self.ensure_one()
        Task = self.env['project.task']
        ids = Task.sudo().with_context(active_test=False)._search(self._woow_project_ai_consult_task_domain())
        return Task.search([('id', 'in', ids), ('woow_project_ai_consult_entry', '=', True)] + (extra_domain or []),
                           order='id desc', limit=limit)

    # --- Hooks (model level) ---

    @api.model
    def _woow_project_ai_consult_fields(self, tab):
        """Names of the fields sent to the AI for `tab` ('internal' or
        'customer'). Each must pass check_field_rules."""
        return ()

    @api.model
    def _woow_project_ai_consult_detail_fields(self, tab):
        """The long fields among _woow_project_ai_consult_fields(tab) (up to
        1,500 characters each; trimmed first)."""
        return ()

    @api.model
    def _woow_project_ai_consult_tabs(self):
        """The tabs these records are sent to: the internal tab, plus the
        customer tab when the model declares customer fields. A model that
        overrides _woow_project_ai_consult_values instead of declaring its
        customer fields overrides this too: a tab not listed here is neither
        sent the records nor gated by them."""
        tabs = {TAB_INTERNAL}
        if self._woow_project_ai_consult_fields(TAB_CUSTOMER):
            tabs.add(TAB_CUSTOMER)
        return tabs

    @api.model
    def _woow_project_ai_consult_access_fields(self):
        """Fields whose change may change who can read a record (a write that
        names one syncs the chats that consulted it)."""
        return {'company_id'} & set(self._fields)

    @api.model
    def _woow_project_ai_consult_readable_query(self, user, tab, candidates):
        """The one readability source of this model (called in `user`'s own
        environment): a Query, an SQL sub-select usable after IN (in
        parentheses), or None (nothing readable). Default: Odoo's own rules as
        `user`. An override must honour `candidates` (always an SQL sub-select
        usable after IN, ``x IN %s``; None for all) and must be uncorrelated
        SQL (it is embedded in every channel query of system administrators);
        a NULL in its result never makes a record readable."""
        return default_readable_query(self, user, tab, candidates)

    @api.model
    def _woow_project_ai_consult_project_from_record(self):
        """True when the consultation project comes from the record itself
        (_woow_project_ai_consult_project), not from the profile."""
        return False

    @api.model
    def _woow_project_ai_consult_profile_warnings(self, profile):
        """Extra fixed sentences shown on the profile form (never a URL or a key)."""
        return []

    # --- Hooks (record level) ---

    def _woow_project_ai_consult_kind(self, lang):
        """What kind of record this is, in `lang` (default: the model's name)."""
        return self.env['ir.model'].sudo().with_context(lang=lang)._get(self._name).name or self._name

    def _woow_project_ai_consult_values(self, user, tab, audience):
        """[(label, text, is_detail)] of this record for the AI of `tab`: the
        declared fields read as `user` (field labels in their language; names
        of related records only those every user of `audience` can read too, at
        most 10). Empty values are left out. The caller cleans and caps."""
        self.ensure_one()
        record = self.with_user(user)
        names = check_field_rules(record, record._woow_project_ai_consult_fields(tab))
        details = set(record._woow_project_ai_consult_detail_fields(tab))
        labels = {}
        for name in names:
            labels.setdefault(record._fields[name]._description_string(record.env), []).append(name)
        values = []
        for name in names:
            field = record._fields[name]
            label = field._description_string(record.env)
            if len(labels[label]) > 1:
                label = '%s (%s)' % (label, name)
            try:
                text = field_text(record, name, audience_filter=lambda found: shared_with(found, audience),
                                  max_names=MAX_RELATED_NAMES, more=MORE_NAMES)
            except (AccessError, MissingError):
                continue
            if text:
                values.append((label, text, name in details))
        return values

    def _woow_project_ai_consult_project(self, user):
        """The consultation project of this record (sudo): the profile's."""
        self.ensure_one()
        profile = self.env[PROFILE].sudo().search([('model', '=', self._name)], limit=1)
        return profile.project_id

    def _woow_project_ai_consult_task_values(self):
        """Values of a new consultation task (the link to this record)."""
        raise NotImplementedError('%s: _woow_project_ai_consult_task_values' % self._name)

    def _woow_project_ai_consult_task_domain(self):
        """Domain of the tasks linked to this record (searched with sudo)."""
        raise NotImplementedError('%s: _woow_project_ai_consult_task_domain' % self._name)

    def _woow_project_ai_consult_task_name(self):
        """Name of a new consultation task, in the environment's language (the
        caller sets the company's language, as for the chat names). Set once
        when the task is created: renaming the record later does not rename
        the task.

        N6 (owner, 2026-11-25: option B): the record's own name, as in
        「AI 顧問：大安區廚房翻新」, read with the asker's own rights (never sudo:
        the asker can read the record, or they could not consult it), cleaned
        as a one-line data value and cut at 80 characters; without a name, the
        kind and the number, as in 「AI 顧問：商機 #42」. Everyone who can read
        the consultation project reads its task names, also colleagues who
        cannot read the record, and names typed by website visitors appear:
        the owner accepted both. This method is the one place to change it."""
        self.ensure_one()
        name = ''
        if self.env.uid:
            try:
                name = cap(clean_data_text(self.with_user(self.env.uid).display_name, one_line=True),
                           MAX_TASK_NAME)
            except AccessError:
                name = ''
        if name:
            return self.env._('AI consultation: %(name)s', name=name)
        return self.env._('AI consultation: %(kind)s #%(id)s', kind=self._woow_project_ai_consult_kind(
            self.env.lang), id=self.id)

    def _woow_project_ai_consult_link_hosts(self):
        """Hosts whose links stay clickable (and whose files may be
        downloaded, when the AI connection allows them too) in the AI replies
        of the chats that consulted these records. Called on the records of
        one model of a chat at once, primary first."""
        return set()

    # --- Membership triggers ---

    def write(self, vals):
        res = super().write(vals)
        if self._woow_project_ai_consult_access_fields() & set(vals) and consult_state(self.env).used:
            # who may read these records may have changed: the chats that consulted them follow
            self.env['discuss.channel']._woow_project_ai_sync_consulted(self)
        return res

    def unlink(self):
        Channel = self.env['discuss.channel']
        chats = Channel._woow_project_ai_consulted_chats(self) if consult_state(self.env).used else None
        res = super().unlink()
        if chats:
            # a deleted record is readable by nobody: the chats that consulted it close (their history stays)
            Channel._woow_project_ai_forget_readable()
            chats.exists()._woow_project_ai_sync()
        return res

    # --- Buttons ---

    def action_woow_project_ai_consult(self):
        """「詢問 AI 顧問」: the internal AI chat of the latest open consultation
        task of this record the user may use, else of a new one."""
        return self._woow_project_ai_consult_open(new=False)

    def action_woow_project_ai_consult_new(self):
        """「開新議題」: always a new consultation task."""
        return self._woow_project_ai_consult_open(new=True)

    def action_woow_project_ai_consult_tasks(self):
        """「AI 顧問」 smart button: the consultation tasks of this record the user can read."""
        self.ensure_one()
        require_int_ids(self)
        self.check_access('read')
        tasks = self._woow_project_ai_consult_readable_tasks()
        # Odoo's flat "All Tasks" list: the default task list groups by stage, and a new consultation project
        # without stages would show one folded group and no row
        flat = self.env.ref('project.open_view_all_tasks_list_view', raise_if_not_found=False)
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('AI Consultations'),
            'res_model': 'project.task',
            'view_mode': 'list,form',
            'views': [(flat.id if flat else False, 'list'), (False, 'form')],
            'domain': [('id', 'in', tasks.ids)],
            'context': {'create': False},
        }

    def _woow_project_ai_consult_open(self, new):
        self.ensure_one()
        require_int_ids(self)
        self.check_access('read')  # the current user's own rights (an RPC: never sudo here)
        user = self.env.user
        Task = self.env['project.task']
        _ = self.env._
        profile = self.env[PROFILE].sudo().search([('model', '=', self._name)], limit=1)
        if not profile.enabled or self._name not in consult_state(self.env).enabled_models:
            raise UserError(_('A system administrator has not turned on the AI consultant for this kind of record '
                              'yet.'))
        if not (user._is_internal() and user.has_group(PROJECT_USER_GROUP)):
            raise UserError(_('You need the user rights of the Project app to use the AI consultant. Please ask a '
                              'system administrator.'))
        project = self._woow_project_ai_consult_project(user).sudo()
        if not project or not project.active:
            raise UserError(_('No consultation project is set yet. A system administrator chooses one in "AI '
                              'Consultant Settings".'))
        companies = company_ids(user)
        if not project.with_user(user).with_context(active_test=False,
                                                    allowed_company_ids=companies).has_access('read'):
            raise UserError(_('You have no access to this consultation project. Please ask a project manager to '
                              'add you to the project.'))
        if not project._woow_project_ai_tab_assistant(TAB_INTERNAL):
            raise UserError(_('The consultation project has no internal AI assistant yet. A project manager '
                              'chooses one on the "AI Assistants" page of the project.'))
        reasons = project._woow_project_ai_tab_blocked_reasons(TAB_INTERNAL)
        if reasons:
            raise UserError('\n'.join(reasons))
        if self.id not in readable_ids(self.env, self._name, [self.id], user, TAB_INTERNAL, companies):
            raise AccessError(_('You have no access to read this record.'))
        task = Task.sudo() if new else self._woow_project_ai_consult_continue(user, project)
        if not task:
            task = self._woow_project_ai_consult_new_task(user, project)
        channel = task._woow_project_ai_open_tab_for(user, TAB_INTERNAL)
        # the record this chat is about goes into its ledger now, not only once its AI gets it (a blocked tab, the
        # caps): unlinking, deleting or disabling it later never opens the chat to people who cannot read it
        self.env[LEDGER].sudo()._woow_project_ai_record(channel, [(self._name, self.id)])
        return {
            'type': 'ir.actions.client',
            'tag': 'woow_project_ai_open_consult_chat',
            'params': {'channel_id': channel.id, 'task_id': task.id},
        }

    def _woow_project_ai_consult_continue(self, user, project):
        """The newest open consultation task of this record in `project` that
        `user` can read and may use (at most 20 looked at), or empty (sudo)."""
        candidates = self._woow_project_ai_consult_readable_tasks(
            [('project_id', '=', project.id), ('is_closed', '=', False)], limit=MAX_CONTINUE_CANDIDATES)
        for task in candidates.sudo():
            if task._woow_project_ai_user_allowed(user, TAB_INTERNAL):
                return task
        return self.env['project.task'].sudo()

    def _woow_project_ai_consult_new_task(self, user, project):
        """A new consultation task for `user` in `project` (sudo, from a
        trusted context in this module's own sync mode: of what the client
        sent with its call only the language and the time zone reach the task,
        contract 3.5), rolled back when the user could not use its internal
        chat (an invited users project they do not follow, a site whose own
        rules decide)."""
        from .discuss_channel import trusted, with_sync  # noqa: PLC0415 - discuss_channel imports this module

        Assistant = self.env['woow.project.ai.assistant']
        company = project.company_id or self.env.company
        lang = Assistant._installed_lang(company.partner_id.lang, self.env.lang)
        vals = dict(self._woow_project_ai_consult_task_values())
        vals.update({
            'name': self.with_context(lang=lang)._woow_project_ai_consult_task_name(),
            'project_id': project.id,
            'user_ids': [Command.set([user.id])],
            'woow_project_ai_consult_entry': True,
        })
        try:
            with self.env.cr.savepoint():
                task = with_sync(trusted(self.env['project.task'].sudo())).create(vals)
                task.message_subscribe(partner_ids=user.partner_id.ids)
                if not task._woow_project_ai_user_allowed(user, TAB_INTERNAL):
                    raise _Refused()
        except _Refused:
            raise UserError(self.env._('You have no access to this consultation project. Please ask a project '
                                       'manager to add you to the project.')) from None
        _logger.info("Project AI consultant: task %s created for %s %s by user %s", task.id, self._name, self.id,
                     user.id)
        return self.env['project.task'].sudo().browse(task.id)


class _Refused(Exception):
    """Rolls the savepoint of a new consultation task back."""
