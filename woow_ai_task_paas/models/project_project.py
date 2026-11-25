# -*- coding: utf-8 -*-
"""Projects on the PaaS platform.

* **The agent runtime never turns on** while the old task AI is off
  (models/legacy.py): ``create`` writes ``use_agent_runtime`` False (an
  explicit key, so it also beats ``default_*`` and ``ir.default``: that
  covers the platform's own automatic enabling and copies); a ``write``
  that would turn it on from False is refused for everyone but the superuser
  mode, and under ``env.su`` (platform code, a cron: never refused) it is
  written False for those projects and logged. True -> True is allowed, and
  turning it off is the platform's own guard's business; no value is ever
  cleared (the platform's reconciler keys on it). A platform migration runs
  during an upgrade before this module is loaded: it is not corrected.
* **Only project managers and administrators configure the project AI**
  (the assistants, the two project texts, the welcome message, the pause).
  woow_ai_task relies on Odoo's own ACL, where only project managers write
  projects; the platform lets every employee write projects, so this
  PaaS-only module adds the guard: a create, a write or a ``default_*`` key
  that sets one of these fields to something else than its default (as
  Odoo would store it) is refused for other users, and their copies drop
  the fields (a copy without AI settings, instead of a refused copy). Odoo's
  own access checks run first. The form shows the fields read-only to them,
  in code (``_get_view``, the user's right in the view cache key).
* **Membership follows the binding**: a change of ``cloud_service_ids`` (or
  of the deprecated ``cloud_service_id``) changes who reads the tasks of a
  bound project (the platform's rules), so the task AI chats are synced
  (fail-soft, models/sync.py).

Nothing here is stored in the database that needs this module's code (no
view, field, rule, filter, action or cron of its own).
"""

import logging

from odoo import api, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import SQL

from odoo.addons.woow_ai_task.models.project_project import DEFAULT_PAUSE_MINUTES

from .legacy import legacy_task_ai_enabled
from .sync import quietly

_logger = logging.getLogger(__name__)

# the project AI settings only project managers and administrators set (woow_ai_task models/project_project.py)
CONFIG_FIELDS = (
    'woow_project_ai_internal_assistant_id',
    'woow_project_ai_customer_assistant_id',
    'woow_project_ai_internal_brief',
    'woow_project_ai_customer_brief',
    'woow_project_ai_customer_welcome',
    'woow_project_ai_pause_minutes',
)
CONFIG_DEFAULTS = {'woow_project_ai_pause_minutes': DEFAULT_PAUSE_MINUTES}
BINDING_FIELDS = {'cloud_service_ids', 'cloud_service_id'}
PROJECT_FORM_HIDDEN = "//field[@name='use_agent_runtime']"
# the six settings on woow_ai_task's "AI Assistants" page, not those of an x2many's own embedded views
PROJECT_FORM_CONFIG = "//field[@name='%s'][not(ancestor::field)]"


def _value_key(field, value):
    """The value Odoo would store for a CONFIG_FIELDS field, in a comparable
    form (an id or False, an integer, a text or ''), as the field's own
    conversion reads it (``convert_to_cache``); None when that is not certain
    (a change, for the guards). Stored values read in SQL pass through it
    too. For a many2one the ORM takes only an exact int (or a record) as an
    id: a digit string, a float or a boolean is stored as no record. For a
    text only None and False are empty: anything else is stored as its
    ``str()`` (0 as '0', [] as '[]')."""
    if field.type == 'many2one':
        if value is None or value is False:
            return False
        if isinstance(value, models.BaseModel):
            if not value:
                return False
            return value.id if len(value) == 1 and type(value.id) is int else None
        if type(value) is int:
            return value or False
        return None
    if field.type == 'integer':
        if isinstance(value, dict):
            return None
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return None
    if value is None or value is False:
        return ''
    return value if isinstance(value, str) else None


class ProjectProject(models.Model):
    _inherit = 'project.project'

    def _woow_project_ai_paas_privileged(self):
        """Who may set the project AI settings: the superuser mode, system
        administrators and project managers."""
        user = self.env.user
        return bool(self.env.su or user.has_group('base.group_system')
                    or user.has_group('project.group_project_manager'))

    def _woow_project_ai_paas_config_denied(self):
        raise AccessError(self.env._('Only project managers can set the AI assistants and the project texts of a '
                                     'project.'))

    # --- create, copy, write ---

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = list(vals_list)  # any iterable (a one-shot generator too): read twice below
        if not self._woow_project_ai_paas_privileged():
            for vals in vals_list:
                # what Odoo will actually write: the values, then default_* context keys and ir.default
                resolved = self._add_missing_default_values(vals)
                for name in CONFIG_FIELDS:
                    if name not in self._fields or name not in resolved:
                        continue
                    field = self._fields[name]
                    if _value_key(field, resolved[name]) != _value_key(field, CONFIG_DEFAULTS.get(name)):
                        self._woow_project_ai_paas_config_denied()
        if 'use_agent_runtime' in self._fields and not legacy_task_ai_enabled(self.env):
            vals_list = [dict(vals, use_agent_runtime=False) for vals in vals_list]
        return super().create(vals_list)

    def copy_data(self, default=None):
        if self._woow_project_ai_paas_privileged():
            return super().copy_data(default=default)
        # a copy by another user has no project AI settings (it would be refused with them). Their defaults go in
        # `default`, so that Odoo never reads the fields for this user: an employee without Project rights may
        # duplicate a project on the platform, but may not read these fields
        default = dict(default or {})
        for name in CONFIG_FIELDS:
            if name in self._fields:
                default[name] = CONFIG_DEFAULTS.get(name, False)
        return super().copy_data(default=default)

    def write(self, vals):
        if vals.get('use_agent_runtime') and 'use_agent_runtime' in self._fields \
                and not legacy_task_ai_enabled(self.env):
            turning_on = self.filtered(lambda project: not project.use_agent_runtime)
            if turning_on:
                if not self.env.su:
                    raise UserError(self.env._('The old task AI is turned off: the Agent Runtime cannot be turned '
                                               'on for a project.'))
                # platform code, a migration or a cron: corrected, never refused
                _logger.warning("woow_ai_task_paas: use_agent_runtime kept False on project(s) %s (the old task AI "
                                "is off)", turning_on.ids)
                result = turning_on.write(dict(vals, use_agent_runtime=False))
                rest = self - turning_on
                return (rest.write(vals) if rest else True) and result
        self._woow_project_ai_paas_check_config_write(vals)
        result = super().write(vals)
        if BINDING_FIELDS & set(vals):
            # a bound project's tasks are read by the members of its services' workspaces
            quietly(self.env, 'the task chat sync after a project binding write', self.ids,
                    lambda: self.env['discuss.channel']._woow_project_ai_sync_projects(self.exists()))
        return result

    def _woow_project_ai_paas_check_config_write(self, vals):
        touched = [name for name in CONFIG_FIELDS if name in vals and name in self._fields]
        if not touched or self._woow_project_ai_paas_privileged():
            return
        # Odoo's own checks first: on a project the user may not write, or for fields outside their groups, the
        # answer is Odoo's error whatever the values (no hint about what is stored)
        self.check_access('write')
        self.check_field_access_rights('write', touched)
        ids = [record_id for record_id in self.ids if isinstance(record_id, int)]
        stored = {}
        if ids:
            # the stored values, read in SQL: a sudo read would put this user's projects in the shared cache, and
            # code after this override (the platform's own) must still meet the user's access checks
            self.flush_recordset(touched)
            self.env.cr.execute(SQL("SELECT id, %s FROM project_project WHERE id IN %s",
                                    SQL(', ').join(SQL.identifier(name) for name in touched), tuple(ids)))
            stored = {row[0]: row[1:] for row in self.env.cr.fetchall()}
        for record_id in ids:
            row = stored.get(record_id) or (None,) * len(touched)
            for name, old in zip(touched, row):
                field = self._fields[name]
                new = _value_key(field, vals[name])
                if new is None or new != _value_key(field, old):
                    self._woow_project_ai_paas_config_denied()

    # --- backend form, in code (the switch and the user's right are part of the view cache key) ---
    # Never an ir.ui.view record: a stored view is applied wherever its row is, and one naming what only this
    # module's code defines stops the form in an image without the module (a rollback image): Odoo's web client
    # refuses a view field the model does not have.

    @api.model
    def _get_view(self, view_id=None, view_type='form', **options):
        arch, view = super()._get_view(view_id, view_type, **options)
        if view_type != 'form':
            return arch, view
        if not legacy_task_ai_enabled(self.env):
            nodes = arch.xpath(PROJECT_FORM_HIDDEN)
            if not nodes:
                _logger.debug("woow_ai_task_paas: project form view %s has no %s", view.id, PROJECT_FORM_HIDDEN)
            for node in nodes:
                node.set('invisible', '1')
        if not self._woow_project_ai_paas_privileged():
            # read-only for the users the write guard refuses (the guard decides; this only says so)
            for name in CONFIG_FIELDS:
                for node in arch.xpath(PROJECT_FORM_CONFIG % name):
                    node.set('readonly', '1')
        return arch, view

    @api.model
    def _get_view_cache_key(self, view_id=None, view_type='form', **options):
        key = super()._get_view_cache_key(view_id, view_type, **options)
        return key + (legacy_task_ai_enabled(self.env), self._woow_project_ai_paas_privileged())
