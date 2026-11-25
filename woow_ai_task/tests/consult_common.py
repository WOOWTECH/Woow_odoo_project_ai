# -*- coding: utf-8 -*-
"""Shared fixtures of the AI consultant tests, for this module and for the
consultant bridges (woow_ai_task_crm, the PaaS bridge).

* ``ConsultFixtures``: the project AI fixtures plus a consultation project.
* ``standin(case)``: without any bridge, res.partner (through the task's
  ``partner_id``) and project.tags (through ``tag_ids``, a many2many) act as
  consulted models for the core tests: the mixin's own hooks are patched onto
  their registry classes and the mixin's inheritance tree lists them
  (patches on the registry classes only, never an import path).
* ``assert_field_rules(case, users)``: every declared field of every mixin
  model passes the field rules, and every related model of a declared field
  is readable by each of `users` who can read the consulted model.
"""

import contextlib
from unittest.mock import patch

from odoo.tools import SQL, OrderedSet

from odoo.addons.woow_ai_task.models.consult_mixin import (
    LEDGER, MIXIN, PROFILE, LinkSpec, consult_models, field_rule_problem)
from odoo.addons.woow_ai_task.models.discuss_channel import with_sync

from odoo.tests.common import TransactionCase

from .common import AiTaskFixtures

# the mixin's hooks a stand-in model gets (the write / unlink overrides call super(): not copied)
HOOKS = (
    '_woow_project_ai_consult_fields', '_woow_project_ai_consult_detail_fields', '_woow_project_ai_consult_tabs',
    '_woow_project_ai_consult_access_fields', '_woow_project_ai_consult_readable_query',
    '_woow_project_ai_consult_project_from_record', '_woow_project_ai_consult_profile_warnings',
    '_woow_project_ai_consult_kind', '_woow_project_ai_consult_values', '_woow_project_ai_consult_project',
    '_woow_project_ai_consult_task_name', '_woow_project_ai_consult_link_hosts',
    '_woow_project_ai_consult_readable_tasks', '_woow_project_ai_consult_allowed',
    '_woow_project_ai_consult_open', '_woow_project_ai_consult_continue', '_woow_project_ai_consult_new_task',
    'action_woow_project_ai_consult', 'action_woow_project_ai_consult_new', 'action_woow_project_ai_consult_tasks',
)
PARTNER_FIELDS = ('name', 'function', 'category_id', 'comment')
PARTNER_DETAILS = ('comment',)
PARTNER_SPEC = LinkSpec('partner_id', 'primary', 10, True)
TAG_SPEC = LinkSpec('tag_ids', 'extra', 20, True)
SECRET_TAG = 'Secret Tag'


@contextlib.contextmanager
def standin(case, tag_readable=None):
    """res.partner and project.tags as consulted models (internal tab only),
    linked from the task by ``partner_id`` (primary) and ``tag_ids``
    (extra). `tag_readable` (a function of the user) replaces the tags'
    readability source: by default every tag but SECRET_TAG is readable."""
    env = case.env
    Mixin = type(env[MIXIN])
    Partner = type(env['res.partner'])
    Tags = type(env['project.tags'])
    Task = type(env['project.task'])

    def tag_query(self, user, tab, candidates):
        if tag_readable is not None:
            return tag_readable(self, user, tab, candidates)
        if not user or not user._is_internal():
            return None
        if candidates is None:
            return SQL("(SELECT id FROM project_tags WHERE name->>'en_US' != %s)", SECRET_TAG)
        return SQL("(SELECT id FROM project_tags WHERE name->>'en_US' != %s AND id IN %s)", SECRET_TAG,
                   SQL("%s", tuple(candidates)) if isinstance(candidates, list) else candidates)

    with contextlib.ExitStack() as stack:
        children = OrderedSet(Mixin._inherit_children) | {'res.partner', 'project.tags'}
        stack.enter_context(patch.object(Mixin, '_inherit_children', children))
        for cls in (Partner, Tags):
            for name in HOOKS:
                stack.enter_context(patch.object(cls, name, getattr(Mixin, name), create=True))
        stack.enter_context(patch.object(
            Partner, '_woow_project_ai_consult_fields',
            lambda self, tab: PARTNER_FIELDS if tab == 'internal' else (), create=True))
        stack.enter_context(patch.object(
            Partner, '_woow_project_ai_consult_detail_fields',
            lambda self, tab: PARTNER_DETAILS if tab == 'internal' else (), create=True))
        stack.enter_context(patch.object(
            Partner, '_woow_project_ai_consult_task_values', lambda self: {'partner_id': self.id}, create=True))
        stack.enter_context(patch.object(
            Partner, '_woow_project_ai_consult_task_domain', lambda self: [('partner_id', '=', self.id)],
            create=True))
        stack.enter_context(patch.object(
            Tags, '_woow_project_ai_consult_fields', lambda self, tab: ('name',) if tab == 'internal' else (),
            create=True))
        stack.enter_context(patch.object(Tags, '_woow_project_ai_consult_readable_query', tag_query, create=True))
        stack.enter_context(patch.object(
            Task, '_woow_project_ai_consult_link_specs', lambda self: [PARTNER_SPEC, TAG_SPEC]))
        assert {'res.partner', 'project.tags'} <= consult_models(env)
        yield


class ConsultFixtures(AiTaskFixtures):
    """The project AI fixtures plus a consultation project (internal assistant:
    the LiteLLM one) and helpers."""

    @classmethod
    def setup_consult(cls):
        cls.setup_ai_task()
        cls.consult_project = cls.env['project.project'].create({
            'name': 'AI Consultations',
            'privacy_visibility': 'employees',
            'user_id': cls.manager.id,
            'woow_project_ai_internal_assistant_id': cls.internal_ai.id,
        })
        cls.Profile = cls.env[PROFILE]
        cls.Ledger = cls.env[LEDGER]

    def profile_for(self, model, **vals):
        """The profile of `model` (created when missing), with `vals` written."""
        Profile = self.env[PROFILE].sudo()
        profile = Profile.search([('model', '=', model)], limit=1)
        if not profile:
            profile = Profile.create(dict({'model_id': self.env['ir.model']._get(model).id}, **vals))
        elif vals:
            profile.write(vals)
        return profile

    def enable(self, model, project=None, **vals):
        return self.profile_for(model, enabled=True, project_id=(project or self.consult_project).id, **vals)

    def record(self, channel, *pairs):
        """Ledger rows written by the module's own SQL writer."""
        return self.env[LEDGER].sudo()._woow_project_ai_record(channel, pairs)

    def ledger_rows(self, channel=None):
        query = "SELECT channel_id, res_model, res_id FROM woow_project_ai_consult_sent"
        params = []
        if channel is not None:
            query += " WHERE channel_id = %s"
            params.append(channel.id)
        self.env.cr.execute(query + " ORDER BY id", params)
        return self.env.cr.fetchall()

    def link(self, task, **vals):
        """Write consultant links as this module's own code does (sync: no guard)."""
        with_sync(task.sudo()).write(vals)


def assert_field_rules(case, users, models=None):
    """Every declared field of every mixin model (or of `models`) passes the
    field rules, and its related model (relational fields) is readable by
    each of `users` who can read the consulted model."""
    env = case.env
    for model in sorted(models or consult_models(env)):
        Model = env[model]
        for tab in ('internal', 'customer'):
            for name in Model._woow_project_ai_consult_fields(tab):
                problem = field_rule_problem(Model, name)
                case.assertFalse(problem, '%s.%s: %s' % (model, name, problem))
                field = Model._fields[name]
                if not field.relational:
                    continue
                for user in users:
                    if Model.with_user(user).has_access('read'):
                        case.assertTrue(env[field.comodel_name].with_user(user).has_access('read'),
                                        '%s.%s: %s cannot read %s' % (model, name, user.login, field.comodel_name))


class StandinCase(ConsultFixtures, TransactionCase):
    """The consultant fixtures with the stand-in consulted models (res.partner,
    project.tags): partner A of the main company, partner B of company B
    (unreadable for the main company's staff), an open and a secret tag."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_consult()
        cls.company_b = cls.env['res.company'].create({'name': 'Consult Company B'})
        cls.partner_a = cls.env['res.partner'].create({
            'name': 'Lead Partner A', 'function': 'Buyer', 'comment': '<p>Prefers oak.</p>',
            'company_id': cls.env.company.id})
        # a customer of company B: unreadable for users of the main company only (Odoo's partner rule)
        cls.partner_b = cls.env['res.partner'].create({'name': 'Hidden Partner B', 'company_id': cls.company_b.id})
        cls.secret_tag = cls.env['project.tags'].create({'name': SECRET_TAG})
        cls.open_tag = cls.env['project.tags'].create({'name': 'Open Tag'})

    def setUp(self):
        super().setUp()
        stack = contextlib.ExitStack()
        stack.enter_context(standin(self))
        self.addCleanup(stack.close)


def assert_consult_lockstep(case, chats, users):
    """For every user and chat: the record predicate
    (``_woow_project_ai_user_allowed``) and the compiled rule
    (``_woow_project_ai_readable_chat_ids``) agree, and so do Odoo's own
    channel search and a fetch of one of the chat's messages. Odoo lets a
    non-administrator read a group chat only as a member, and the rule lists
    a user's member chats first: each user is made a member first (as this
    module's own sync would do; membership itself never decides the
    predicate)."""
    from odoo.exceptions import AccessError  # noqa: PLC0415

    env = case.env
    Channel = env['discuss.channel']
    for chat in chats:
        chat = chat.sudo()
        task, tab = chat.woow_project_ai_task_id, chat.woow_project_ai_tab
        message = env['mail.message'].sudo().search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', chat.id)], order='id desc', limit=1)
        for user in users:
            if user.partner_id not in chat.channel_member_ids.partner_id:
                with_sync(chat)._add_members(users=user, post_joined_message=False)
            Channel._woow_project_ai_forget_readable()
            env.invalidate_all()
            allowed = task._woow_project_ai_user_allowed(user, tab)
            label = '%s / chat %s (%s)' % (user.login, chat.id, tab)
            case.assertEqual(bool(Channel.with_user(user)._woow_project_ai_readable_chat_ids({chat.id})), allowed,
                             '%s: rule' % label)
            case.assertEqual(bool(Channel.with_user(user).search_count([('id', '=', chat.id)])), allowed,
                             '%s: channel search' % label)
            if message:
                env.invalidate_all()
                try:
                    message.with_user(user).read(['body'])
                    fetched = True
                except AccessError:
                    fetched = False
                case.assertEqual(fetched, allowed, '%s: message fetch' % label)
