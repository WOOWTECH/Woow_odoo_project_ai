# -*- coding: utf-8 -*-
"""The AI consultant's generic mechanism without any bridge (res.partner and
project.tags stand in for consulted models, consult_common.standin): the
state, the profile, the ledger, the consultation flag and the link guard of
tasks, the 「AI 顧問對象」 page."""

from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import new_test_user, tagged
from odoo.tools import SQL, mute_logger

from odoo.addons.base.models.ir_model import MODULE_UNINSTALL_FLAG

from odoo.addons.woow_ai_task.models.consult_mixin import (
    LEDGER, PROFILE, consult_keys, consult_state, field_rule_problem, readable_ids)
from odoo.addons.woow_ai_task.models import consult_mixin as consult_mixin_module
from odoo.addons.woow_ai_task.models import consult_profile as consult_profile_module
from odoo.addons.woow_ai_task.models.consult_profile import TABLES_READY_KEY
from odoo.addons.woow_ai_task.models.discuss_channel import with_sync

from .consult_common import SECRET_TAG, StandinCase, assert_consult_lockstep, assert_field_rules, standin


@tagged('post_install', '-at_install', 'woow_ai_task', 'woow_ai_task_consult')
class TestConsultState(StandinCase):

    def test_unused_costs_nothing(self):
        state = consult_state(self.env)
        self.assertFalse(state.used)
        self.assertFalse(state.enabled_models)
        chat = self.open_tab(self.staff, 'internal')
        task = self.task.sudo()
        consult_state(self.env)  # the state is cached; the tables check is remembered on the registry
        with self.assertQueryCount(0):
            self.assertEqual(task._woow_project_ai_consult_gate('internal'), {})
            self.assertEqual(consult_state(self.env), state)
            self.assertIsNone(chat._woow_project_ai_link_hosts(self.internal_ai))
            self.assertEqual(chat._woow_project_ai_consulted_records(), {})
        self.assertTrue(task._woow_project_ai_consult_user_ok(self.staff, 'internal', [self.env.company.id]))

    def test_state_follows_profiles_in_the_same_transaction(self):
        profile = self.profile_for('res.partner')
        self.assertFalse(consult_state(self.env).enabled_models)
        profile.write({'enabled': True, 'project_id': self.consult_project.id, 'no_data_tools': True})
        state = consult_state(self.env)
        self.assertEqual(state.enabled_models, frozenset({'res.partner'}))
        self.assertTrue(state.used)
        self.assertIn(self.consult_project.id, state.profile_project_ids)
        profile.enabled = False
        state = consult_state(self.env)
        self.assertFalse(state.enabled_models)
        self.assertTrue(state.used, 'a profile enabled once keeps consultation "used"')
        self.assertTrue(profile.was_enabled)
        with self.assertRaises(UserError):
            profile.was_enabled = False

    def test_ledger_rows_never_clear_the_state(self):
        self.enable('res.partner')
        chat = self.open_tab(self.staff, 'internal')
        consult_state(self.env)
        Registry = type(self.env.registry)
        with patch.object(Registry, 'clear_cache', autospec=True) as clear:
            self.assertEqual(self.record(chat, ('res.partner', self.partner_a.id)), ['res.partner'])
            with self.assertQueryCount(0):
                consult_state(self.env)
        clear.assert_not_called()

    def test_missing_tables_read_as_unused(self):
        """The upgrade window (new code, tables not made yet): to_regclass gives NULL, the state reads unused
        and nothing is remembered."""
        self.enable('res.partner')
        registry, cr = self.env.registry, self.env.cr
        original = cr.execute
        looked_up = []

        def execute(query, params=None, log_exceptions=True):
            code = query.code if isinstance(query, SQL) else query
            if 'to_regclass' in code:
                looked_up.append(code)
                return original("SELECT NULL::regclass IS NOT NULL AND NULL::regclass IS NOT NULL", None,
                                log_exceptions)
            return original(query, params, log_exceptions)
        flag = registry.__dict__.pop(TABLES_READY_KEY, None)
        registry.clear_cache()
        cr.execute = execute
        try:
            state = consult_state(self.env)
            remembered = getattr(registry, TABLES_READY_KEY, False)
        finally:
            del cr.execute
            if flag is not None:
                setattr(registry, TABLES_READY_KEY, flag)
            registry.clear_cache()
        self.assertTrue(looked_up, 'the tables were looked up with to_regclass')
        self.assertFalse(state.used)
        self.assertFalse(state.enabled_models)
        self.assertFalse(remembered, 'a missing table is asked again next time')
        self.assertTrue(consult_state(self.env).used, 'with the tables there: enabled once, so used')

    def test_ledger_keys_come_from_the_registry(self):
        # a model's first ledger row is checked against keys of the registry, not of the (cached) state
        state = consult_state(self.env)
        self.assertIn(('res.partner', 'internal'), consult_keys(self.env, state))
        self.assertNotIn(('res.partner', 'customer'), consult_keys(self.env, state), 'no customer fields')
        # a ledger model this registry has no mixin for any more: both tabs, checked by Odoo's own rules
        chat = self.open_tab(self.staff, 'internal')
        self.record(chat, ('res.country', self.env.ref('base.tw').id))
        self.env.registry.clear_cache()
        state = consult_state(self.env)
        self.assertIn('res.country', state.ledger_other_models)
        self.assertTrue({('res.country', 'internal'), ('res.country', 'customer')} <= consult_keys(self.env, state))


@tagged('post_install', '-at_install', 'woow_ai_task', 'woow_ai_task_consult')
class TestConsultProfile(StandinCase):

    def test_only_system_administrators(self):
        profile = self.profile_for('res.partner')
        for user in (self.manager, self.staff, self.employee, self.customer):
            Profile = self.env[PROFILE].with_user(user)
            with self.assertRaises(AccessError, msg=user.login):
                Profile.search([])
            with self.assertRaises(AccessError, msg=user.login):
                profile.with_user(user).write({'enabled': True})
            with self.assertRaises(AccessError, msg=user.login):
                Profile.create({'model_id': self.env['ir.model']._get('project.tags').id})

    def test_rules(self):
        profile = self.profile_for('res.partner')
        with mute_logger('odoo.sql_db'), self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env[PROFILE].create({'model_id': profile.model_id.id})
        with self.assertRaises(ValidationError):
            self.env[PROFILE].create({'model_id': self.env['ir.model']._get('res.country').id})
        with self.assertRaises(ValidationError):
            profile.write({'enabled': True})  # no consultation project
        with self.assertRaises(ValidationError):
            profile.write({'role_prompt': 'x' * 1001})
        with self.assertRaises(ValidationError) as caught:  # (Odoo's assertRaises: in a savepoint)
            profile.write({'project_id': self.consult_project.id, 'enabled': True})
        self.assertIn('no tools that read company data', str(caught.exception))
        profile.write({'role_prompt': 'x' * 1000, 'project_id': self.consult_project.id, 'enabled': True,
                       'no_data_tools': True})
        self.assertTrue(profile.was_enabled)
        # once enabled, a setting is disabled, never deleted (deleting it would forget was_enabled)
        profile.write({'enabled': False})
        with self.assertRaises(UserError):
            profile.unlink()
        self.assertTrue(profile.exists())
        never = self.profile_for('project.tags')
        never.unlink()  # never enabled: may go

    def test_warnings(self):
        profile = self.profile_for('res.partner')
        self.assertIn('No consultation project is chosen yet.', profile.warning)
        project = self.env['project.project'].create({'name': 'Consult without AI'})
        profile.project_id = project
        self.assertIn('cannot answer now', profile.warning)
        self.assertIn("carry the record's name", profile.warning, 'task names carry record names')
        project.write({'woow_project_ai_internal_assistant_id': self.internal_n8n.id, 'privacy_visibility': 'portal',
                       'woow_project_ai_customer_assistant_id': self.customer_ai.id})
        profile.invalidate_recordset(['warning'])
        warning = profile.warning
        self.assertIn('shared with portal users', warning)
        self.assertIn('n8n', warning)
        self.assertIn('no fields for customers', warning)
        Partner = type(self.env['res.partner'])
        with patch.object(Partner, '_woow_project_ai_consult_profile_warnings',
                          lambda self, profile: ['A sentence of the bridge.'], create=True):
            profile.invalidate_recordset(['warning'])
            self.assertIn('A sentence of the bridge.', profile.warning)

    def test_data_destination_has_no_url_or_key(self):
        profile = self.enable('res.partner')
        destination = profile.data_destination
        self.assertIn('Internal Helper', destination)
        self.assertIn('LiteLLM', destination)
        for secret in ('https://', 'llm.example.com', 'test-backend-key'):
            self.assertNotIn(secret, destination)
        self.consult_project.woow_project_ai_internal_assistant_id = self.internal_n8n
        profile.invalidate_recordset(['data_destination'])
        self.assertIn('n8n', profile.data_destination)
        self.assertNotIn('n8n.example.com', profile.data_destination)

    def test_enabling_needs_the_confirmation_and_pins_the_destination(self):
        profile = self.profile_for('res.partner', project_id=self.consult_project.id)
        self.assertFalse(profile.no_data_tools, 'off by default')
        with self.assertRaises(ValidationError) as caught:
            profile.write({'enabled': True})
        self.assertIn('no tools that read company data', str(caught.exception))
        profile.write({'enabled': True, 'no_data_tools': True})
        self.assertTrue(profile.no_data_tools)
        self.assertFalse(profile.destination_changed)
        self.assertEqual(profile.destination_fingerprint, 'assistant:%s;connection:%s;kind:openai;host:llm.example.com'
                         % (self.internal_ai.id, self.internal_ai.connection_id.id))
        # the project's internal assistant changes (project managers choose it): paused until confirmed again
        self.consult_project.woow_project_ai_internal_assistant_id = self.internal_n8n
        profile.invalidate_recordset()
        self.assertTrue(profile.destination_changed)
        self.assertFalse(profile.no_data_tools, 'a new destination clears the confirmation')
        self.assertIn('Where the record data goes changed', profile.warning)
        self.assertIn('(now: Internal Workflow (n8n', profile.warning)
        with self.assertRaises(ValidationError):
            profile.write({'project_id': self.consult_project.id})  # kept enabled without the confirmation
        profile.no_data_tools = True  # a system administrator checked the new AI
        self.assertFalse(profile.destination_changed)
        self.assertTrue(profile.destination_fingerprint.endswith(';kind:n8n;host:n8n.example.com'))
        # the connection's backend host changes: paused again
        self.internal_n8n.connection_id.write({'endpoint_url': 'https://other.example.net/webhook/i',
                                               'api_key': 'other-backend-key'})
        profile.invalidate_recordset()
        self.assertTrue(profile.destination_changed)
        # no assistant at all is a destination as well
        self.consult_project.woow_project_ai_internal_assistant_id = False
        profile.no_data_tools = True
        self.assertEqual(profile.destination_fingerprint, 'none')
        self.consult_project.woow_project_ai_internal_assistant_id = self.internal_ai
        profile.invalidate_recordset()
        self.assertTrue(profile.destination_changed)
        # unticking it is refused while enabled; disabled, the setting may stay unconfirmed
        with self.assertRaises(ValidationError):
            profile.write({'no_data_tools': False, 'enabled': True})
        profile.write({'enabled': False, 'no_data_tools': False})
        self.assertFalse(profile.destination_fingerprint)
        self.assertFalse(profile.destination_changed, 'only an enabled setting is paused')

    def test_notes_are_off_by_default_and_said_so(self):
        profile = self.enable('res.partner')
        self.assertFalse(profile.send_notes)
        self.assertFalse(consult_state(self.env).notes_models)
        self.assertIn('Notes: not sent', profile.data_destination)
        self.assertIn('The Notes are not sent', profile.warning)
        profile.send_notes = True
        profile.invalidate_recordset(['data_destination', 'warning'])
        self.assertIn('Notes: sent, contact details masked', profile.data_destination)
        self.assertIn('E-mail addresses, phone numbers and IP addresses are masked', profile.warning)
        self.assertEqual(consult_state(self.env).notes_models, frozenset({'res.partner'}))
        # a kind of record without long free texts says nothing of Notes
        tags = self.enable('project.tags')
        self.assertNotIn('Notes', tags.data_destination)
        self.assertNotIn('The Notes', tags.warning)

    def test_rights_lines_count_and_follow_default_access_rights(self):
        profile = self.profile_for('res.partner', project_id=self.consult_project.id)
        employees = self.env['res.users'].search([('share', '=', False)])
        managers = employees.filtered(lambda user: user.has_group('project.group_project_manager'))
        self.assertTrue(managers)
        self.assertIn('Employees who are Project Administrators (they read every consultation task): %s of %s.'
                      % (len(managers), len(employees)), profile.warning)
        self.assertIn('Project Administrators read every task whatever the project', profile.warning)
        Param = self.env['ir.config_parameter'].sudo()
        Param.set_param('base_setup.default_user_rights', 'True')
        profile.invalidate_recordset(['warning'])
        self.assertIn('Default Access Rights (Settings > Users) is on', profile.warning)
        Param.set_param('base_setup.default_user_rights', False)
        profile.invalidate_recordset(['warning'])
        self.assertNotIn('Default Access Rights', profile.warning)

    def test_enabled_once_setting_goes_only_with_its_module(self):
        profile = self.enable('res.partner')
        profile.enabled = False
        with self.assertRaises(UserError):
            profile.unlink()
        # uninstalling the module that made it (Odoo deletes module data with this flag)
        profile.with_context(**{MODULE_UNINSTALL_FLAG: True}).unlink()
        self.assertFalse(profile.exists())

    def test_enabling_runs_the_sweep_and_forgets_readable(self):
        profile = self.profile_for('res.partner', project_id=self.consult_project.id, no_data_tools=True)
        cron = self.env.ref('woow_ai_task.ir_cron_project_ai_sync_members')
        Cron = type(self.env['ir.cron'])
        Channel = type(self.env['discuss.channel'])
        with patch.object(Cron, '_trigger', autospec=True) as trigger, \
                patch.object(Channel, '_woow_project_ai_forget_readable', autospec=True) as forget:
            profile.enabled = True
        self.assertEqual([call.args[0] for call in trigger.call_args_list], [cron])
        self.assertTrue(forget.called)
        with patch.object(Cron, '_trigger', autospec=True) as trigger:
            profile.role_prompt = 'Only the prompt changed.'
        trigger.assert_not_called()

    def test_preview_lists_who_would_leave_by_name(self):
        # the staff of company A reads partner A, nobody of the chat reads partner B; staff2 reads neither
        # once restricted to company B... here: a chat whose task links partner B (company B)
        self.consult_project.user_id = self.manager
        task = self.env['project.task'].create({'name': 'Consult B', 'project_id': self.project.id,
                                                'user_ids': [Command.set([self.staff.id])]})
        chat = self.open_tab(self.staff, 'internal', task=task)
        both = new_test_user(self.env, 'consult_both', name='Both Bea', groups='base.group_user,'
                             'project.group_project_user', company_ids=[Command.set([self.env.company.id,
                                                                                    self.company_b.id])])
        task.message_subscribe(partner_ids=both.partner_id.ids)
        self.open_tab(both, 'internal', task=task)
        self.link(task, partner_id=self.partner_b.id)
        profile = self.profile_for('res.partner', project_id=self.consult_project.id)
        count, capped, leaving = profile._woow_project_ai_consult_preview_counts()
        self.assertEqual(count, 1)
        self.assertFalse(capped)
        self.assertEqual({partner.name: chats for partner, chats in leaving.items()}, {'Staff Sam': 1})
        text = profile._woow_project_ai_consult_preview_text()
        self.assertIn('- Staff Sam: 1 chat', text.split('\n'))
        self.assertNotIn('Both Bea', text)
        with patch.object(consult_profile_module, 'PREVIEW_MAX_NAMES', 0):
            self.assertIn('… and 1 more person.', profile._woow_project_ai_consult_preview_text())
        action = profile.action_woow_project_ai_consult_preview()
        self.assertEqual(action['res_model'], 'woow.project.ai.consult.preview')
        self.assertEqual(action['target'], 'new')
        # nothing changed
        self.assertIn(self.staff.partner_id, chat.sudo().channel_member_ids.partner_id)
        self.assertFalse(profile.enabled)


@tagged('post_install', '-at_install', 'woow_ai_task', 'woow_ai_task_consult')
class TestConsultLedger(StandinCase):

    def test_orm_refused_for_everyone(self):
        chat = self.open_tab(self.staff, 'internal')
        self.record(chat, ('res.partner', self.partner_a.id))
        row = self.env[LEDGER].sudo().search([])
        admin = self.env.ref('base.user_admin')
        for env in (self.env(user=admin.id), self.env(user=1), self.env(su=True)):
            Ledger = env[LEDGER]
            with self.assertRaises(AccessError):
                Ledger.create({'channel_id': chat.id, 'res_model': 'res.partner', 'res_id': self.partner_b.id})
            with self.assertRaises(AccessError):
                row.with_env(env).write({'res_id': self.partner_b.id})
            with self.assertRaises(AccessError):
                row.with_env(env).unlink()
        self.assertEqual(row.display_name, 'res.partner,%s' % self.partner_a.id)
        # only the module's own sync mode may use the ORM
        with_sync(self.env[LEDGER].sudo()).create({'channel_id': chat.id, 'res_model': 'res.partner',
                                                   'res_id': self.partner_b.id})

    def test_writer_takes_task_chats_only_once(self):
        plain = self.env['discuss.channel'].create({'name': 'plain group', 'channel_type': 'group'})
        self.assertEqual(self.record(plain, ('res.partner', self.partner_a.id)), [])
        self.assertFalse(self.ledger_rows(plain))
        chat = self.open_tab(self.staff, 'internal')
        self.assertEqual(self.record(chat, ('res.partner', self.partner_a.id), ('res.partner', self.partner_a.id)),
                         ['res.partner'])
        self.assertEqual(self.record(chat, ('res.partner', self.partner_a.id)), [], 'unique per chat')
        self.assertEqual(self.ledger_rows(chat), [(chat.id, 'res.partner', self.partner_a.id)])
        # a float or a string id is the same record; anything else is ignored
        self.assertEqual(self.record(chat, ('res.partner', float(self.partner_b.id)), ('res.partner', 'x')),
                         ['res.partner'])
        # the rows go with the chat (its task deleted)
        self.task.unlink()
        self.assertFalse(self.ledger_rows())

    def test_mover_collapses_and_forgets(self):
        chat1 = self.open_tab(self.staff, 'internal')
        task2 = self.env['project.task'].create({'name': 'Second', 'project_id': self.project.id,
                                                 'user_ids': [Command.set([self.staff.id])]})
        chat2 = self.open_tab(self.staff, 'internal', task=task2)
        keep = self.env['res.partner'].create({'name': 'Kept Partner'})
        self.record(chat1, ('res.partner', self.partner_a.id), ('res.partner', keep.id))
        self.record(chat2, ('res.partner', self.partner_a.id), ('res.partner', self.partner_b.id))
        Channel = type(self.env['discuss.channel'])
        with patch.object(Channel, '_woow_project_ai_forget_readable', autospec=True) as forget:
            moved = self.env[LEDGER].sudo()._woow_project_ai_move(
                'res.partner', [self.partner_a.id, self.partner_b.id, keep.id], keep.id)
        self.assertTrue(forget.called)
        self.assertEqual(sorted(moved), sorted([chat1.id, chat2.id]))
        self.assertEqual(sorted(self.ledger_rows()), sorted([(chat1.id, 'res.partner', keep.id),
                                                             (chat2.id, 'res.partner', keep.id)]))


@tagged('post_install', '-at_install', 'woow_ai_task', 'woow_ai_task_consult')
class TestConsultTaskGuards(StandinCase):

    def entry_refused(self):
        return self.assertRaisesRegex(AccessError, 'AI consultation topic')

    def test_consultation_flag_only_from_the_button(self):
        Task = self.env['project.task'].with_user(self.manager)
        vals = {'name': 'Flagged?', 'project_id': self.project.id}
        with self.entry_refused():
            Task.create(dict(vals, woow_project_ai_consult_entry=True))
        with self.entry_refused():
            Task.with_context(default_woow_project_ai_consult_entry=True).create(vals)
        task = Task.create(vals)
        with self.entry_refused():
            task.write({'woow_project_ai_consult_entry': True})
        self.env['ir.default'].set('project.task', 'woow_project_ai_consult_entry', True, user_id=self.manager.id)
        with self.entry_refused():
            Task.create(vals)
        self.env['ir.default'].discard_values('project.task', 'woow_project_ai_consult_entry', [True])
        # this module's own code (the button)
        flagged = with_sync(self.env['project.task'].sudo()).create(dict(vals, woow_project_ai_consult_entry=True))
        self.assertTrue(flagged.woow_project_ai_consult_entry)
        self.assertFalse(task.woow_project_ai_consult_entry)

    def assert_refused(self, call):
        with self.assertRaises(AccessError) as caught, self.env.cr.savepoint():
            call()
        self.assertNotIn('Hidden Partner B', str(caught.exception))
        self.assertNotIn(SECRET_TAG, str(caught.exception))
        return caught.exception

    def test_link_guard_refuses_unreadable_records(self):
        self.assertFalse(readable_ids(self.env, 'res.partner', [self.partner_b.id], self.staff, 'internal',
                                      [self.env.company.id]))
        Task = self.env['project.task'].with_user(self.staff)
        vals = {'name': 'Linked', 'project_id': self.project.id, 'user_ids': [Command.set([self.staff.id])]}
        for value in (self.partner_b.id, float(self.partner_b.id), str(self.partner_b.id)):
            self.assert_refused(lambda value=value: Task.create(dict(vals, partner_id=value)))
        self.assert_refused(lambda: Task.with_context(default_partner_id=self.partner_b.id).create(vals))
        self.env['ir.default'].set('project.task', 'partner_id', self.partner_b.id, user_id=self.staff.id)
        self.assert_refused(lambda: Task.create(vals))
        self.env['ir.default'].discard_values('project.task', 'partner_id', [self.partner_b.id])
        task = Task.create(dict(vals, partner_id=self.partner_a.id))
        self.assert_refused(lambda: task.write({'partner_id': self.partner_b.id}))
        for value in (float(self.partner_b.id), str(self.partner_b.id)):
            with self.env.cr.savepoint():
                try:
                    task.write({'partner_id': value})
                except AccessError:
                    continue
                self.env.flush_all()
                self.env.cr.execute("SELECT partner_id FROM project_task WHERE id = %s", [task.id])
                self.assertNotEqual(self.env.cr.fetchone()[0], self.partner_b.id, repr(value))
        # a many2many link: the secret tag (its readability source refuses it)
        self.assert_refused(lambda: task.write({'tag_ids': [Command.link(self.secret_tag.id)]}))
        self.assert_refused(lambda: task.write({'tag_ids': [Command.set([self.open_tag.id, self.secret_tag.id])]}))
        self.assert_refused(lambda: Task.create(dict(vals, tag_ids=[Command.set([self.secret_tag.id])])))
        task.write({'tag_ids': [Command.link(self.open_tag.id)]})
        self.assertEqual(task.sudo().tag_ids, self.open_tag)

    def test_link_guard_keeps_what_was_there(self):
        # a link the user did not add stays (only added records are checked)
        task = self.task.sudo()
        task.write({'tag_ids': [Command.link(self.secret_tag.id)]})
        self.task.with_user(self.staff).write({'tag_ids': [Command.link(self.open_tag.id)]})
        self.assertEqual(task.tag_ids, self.secret_tag | self.open_tag | self.tag)

    def test_superuser_and_sync_are_exempt(self):
        self.task.sudo().write({'partner_id': self.partner_b.id})
        with_sync(self.task.with_user(self.staff)).write({'partner_id': self.partner_a.id})
        with_sync(self.task.with_user(self.staff)).write({'partner_id': self.partner_b.id})
        self.assertEqual(self.task.sudo().partner_id, self.partner_b)

    def test_link_write_syncs_the_task_chats_once_used(self):
        Channel = type(self.env['discuss.channel'])
        with patch.object(Channel, '_woow_project_ai_sync_tasks', autospec=True) as sync:
            self.task.sudo().write({'partner_id': self.partner_a.id})
        sync.assert_not_called()  # consultation unused: nothing gates
        self.enable('res.partner')
        with patch.object(Channel, '_woow_project_ai_sync_tasks', autospec=True) as sync:
            self.task.sudo().write({'partner_id': False})
        self.assertEqual(sync.call_count, 1)


@tagged('post_install', '-at_install', 'woow_ai_task', 'woow_ai_task_consult')
class TestConsultTaskPage(StandinCase):

    def test_page_comes_from_python_not_from_a_stored_view(self):
        """Review (image rollback): no stored view of another model names a consultant field, so an image
        without this code (an older release, a rollback image) still opens those forms; the page is added by
        project.task._get_view."""
        self.env.flush_all()
        self.env.cr.execute("SELECT model FROM ir_ui_view WHERE arch_db::text LIKE %s",
                            ['%woow_project_ai_consult%'])
        models_ = {row[0] for row in self.env.cr.fetchall()}
        self.assertFalse({model for model in models_ if not model.startswith('woow.project.ai.consult')}, models_)
        arch = self.env['project.task'].with_user(self.staff).get_views([(False, 'form')])['views']['form']['arch']
        self.assertIn('name="woow_project_ai_consult"', arch)
        self.assertIn('name="woow_project_ai_consult_summary"', arch)
        self.assertIn('name="woow_project_ai_consult_targets"', arch)
        employee_arch = self.env['project.task'].with_user(self.employee).get_view(view_type='form')['arch']
        self.assertNotIn('name="woow_project_ai_consult"', employee_arch, 'Project users only')

    def test_summary_names_only_what_the_reader_can_read(self):
        self.enable('res.partner')  # tags stay disabled
        chat = self.open_tab(self.staff, 'internal')
        earlier = self.env['res.partner'].create({'name': 'Earlier Partner'})
        self.record(chat, ('res.partner', earlier.id), ('res.partner', self.partner_b.id))
        self.link(self.task, partner_id=self.partner_a.id, tag_ids=[Command.set([self.open_tag.id,
                                                                                 self.secret_tag.id])])
        summary = self.task.with_user(self.staff).woow_project_ai_consult_summary
        lines = summary.split('\n')
        self.assertIn('Contact: Lead Partner A (primary)', lines)
        self.assertIn('Project Tags: Open Tag (other) (not enabled: the AI does not read it)', lines)
        self.assertIn('Consulted before: Contact: Earlier Partner', lines)
        self.assertIn('Records you cannot read: 2', lines)
        for hidden in ('Hidden Partner B', SECRET_TAG):
            self.assertNotIn(hidden, summary)
        self.assertTrue(self.task.with_user(self.staff).woow_project_ai_consult_show)
        # an employee without Project rights never gets the field
        with self.assertRaises(AccessError):
            self.task.with_user(self.employee).read(['woow_project_ai_consult_summary'])

    def test_show(self):
        task = self.env['project.task'].create({'name': 'Nothing linked', 'project_id': self.project.id,
                                                'user_ids': [Command.set([self.staff.id])]})
        self.assertFalse(task.with_user(self.staff).woow_project_ai_consult_show)
        self.profile_for('res.partner', project_id=self.project.id)
        task.invalidate_recordset(['woow_project_ai_consult_show'])
        self.assertTrue(task.with_user(self.staff).woow_project_ai_consult_show, "a profile's project")
        # a link (here the fixture task's tag: project.tags stands in for a consulted model)
        self.assertTrue(self.task.with_user(self.staff).woow_project_ai_consult_show)

    def test_page_fields_of_many_tasks_read_the_ledger_once(self):
        self.enable('res.partner')
        task2 = self.env['project.task'].create({'name': 'Second', 'project_id': self.project.id,
                                                 'user_ids': [Command.set([self.staff.id])]})
        chat1 = self.open_tab(self.staff, 'internal')
        chat2 = self.open_tab(self.staff, 'internal', task=task2)
        earlier = self.env['res.partner'].create({'name': 'Earlier Partner'})
        self.record(chat1, ('res.partner', earlier.id))
        self.record(chat2, ('res.partner', self.partner_b.id))
        Task = type(self.env['project.task'])
        original = Task._woow_project_ai_consult_ledger_pairs_by_task
        calls = []

        def spy(tasks):
            calls.append(len(tasks))
            return original(tasks)

        tasks = (self.task | task2).with_user(self.staff)
        tasks.invalidate_recordset(['woow_project_ai_consult_show', 'woow_project_ai_consult_summary'])
        with patch.object(Task, '_woow_project_ai_consult_ledger_pairs_by_task', spy):
            rows = {row['id']: row for row in tasks.read(['woow_project_ai_consult_show',
                                                          'woow_project_ai_consult_summary'])}
        self.assertEqual(calls, [2, 2], 'one ledger query per field for the whole recordset')
        self.assertIn('Consulted before: Contact: Earlier Partner',
                      rows[self.task.id]['woow_project_ai_consult_summary'])
        self.assertEqual(rows[task2.id]['woow_project_ai_consult_summary'], 'Records you cannot read: 1')
        self.assertTrue(rows[task2.id]['woow_project_ai_consult_show'])

    def test_rename_leaves_the_old_name_nowhere_the_module_wrote_it(self):
        chat = self.open_tab(self.staff, 'internal')
        self.post(chat, self.staff, 'question')
        self.task.message_post(body='a note', message_type='comment', subtype_xmlid='mail.mt_note')
        old = self.task.name
        self.env['project.task']._woow_project_ai_consult_rename({self.task.id: 'AI consultation: Contact #7'})
        self.assertEqual(self.task.name, 'AI consultation: Contact #7')
        self.assertIn('AI consultation: Contact #7', chat.sudo().name)
        self.assertNotIn(old, chat.sudo().name)
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT count(*) FROM mail_message
             WHERE record_name LIKE %s
               AND ((model = 'discuss.channel' AND res_id = %s) OR (model = 'project.task' AND res_id = %s))""",
                            ['%%%s%%' % old, chat.id, self.task.id])
        self.assertEqual(self.env.cr.fetchone()[0], 0)
        self.assertFalse(self.env['mail.tracking.value'].sudo().search([
            ('mail_message_id.model', '=', 'project.task'), ('mail_message_id.res_id', '=', self.task.id),
            ('old_value_char', '=', old)]), 'no tracking line keeps the old name')

    def test_field_rules(self):
        assert_field_rules(self, [self.staff, self.manager, self.employee], models=['res.partner', 'project.tags'])
        Partner = self.env['res.partner']
        self.assertTrue(field_rule_problem(Partner, 'email'))
        self.assertTrue(field_rule_problem(Partner, 'phone'))
        self.assertTrue(field_rule_problem(Partner, 'image_1920'))
        self.assertTrue(field_rule_problem(Partner, 'street'))
        self.assertTrue(field_rule_problem(Partner, 'no_such_field'))
        self.assertTrue(field_rule_problem(self.env['res.users'], 'login_date'))  # restricted to groups
        self.assertFalse(field_rule_problem(Partner, 'function'))


@tagged('post_install', '-at_install', 'woow_ai_task', 'woow_ai_task_consult')
class TestConsultGate(StandinCase):
    """The gate: the predicate and the compiled rule (§4.6–4.7)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.both = new_test_user(cls.env, 'consult_both', name='Both Bea', groups='base.group_user,'
                                 'project.group_project_user', company_ids=[Command.set([cls.env.company.id,
                                                                                         cls.company_b.id])])
        cls.task.message_subscribe(partner_ids=cls.both.partner_id.ids)
        cls.admin = cls.env.ref('base.user_admin')

    def chat_with(self, *users, tab='internal'):
        chat = self.open_tab(users[0], tab)
        for user in users[1:]:
            self.open_tab(user, tab)
        return chat

    def rule_sql(self, user):
        Channel = self.env['discuss.channel']
        domain = Channel._woow_project_ai_consult_rule_domain(user, [self.env.company.id])
        return Channel.sudo()._search(domain).select().code if domain else ''

    def test_unreadable_record_hides_refuses_and_removes(self):
        chat = self.chat_with(self.staff, self.both)
        self.post(chat, self.staff, 'before')
        self.enable('res.partner')
        self.record(chat, ('res.partner', self.partner_b.id))
        task = self.task.sudo()
        self.assertFalse(task._woow_project_ai_user_allowed(self.staff, 'internal'))
        self.assertTrue(task._woow_project_ai_user_allowed(self.staff, 'internal', consult=False))
        self.assertTrue(task._woow_project_ai_user_allowed(self.both, 'internal'))
        Channel = self.env['discuss.channel']
        Channel._woow_project_ai_forget_readable()
        self.assertFalse(Channel.with_user(self.staff).search([('id', '=', chat.id)]))
        self.assertTrue(Channel.with_user(self.both).search([('id', '=', chat.id)]))
        with self.assertRaisesRegex(AccessError, 'records this task consulted') as caught:
            self.task.with_user(self.staff).woow_project_ai_open_tab('internal')
        self.assertNotIn('Hidden Partner B', str(caught.exception))
        with self.assertRaises(AccessError):
            self.post(chat, self.staff, 'after')
        chat._woow_project_ai_sync()
        self.assertNotIn(self.staff.partner_id, self.members(chat))
        self.assertIn(self.both.partner_id, self.members(chat))

    def test_missing_record_and_unknown_model_fail_closed(self):
        chat = self.chat_with(self.staff, self.both)
        gone = self.env['res.partner'].create({'name': 'Gone Partner'})
        self.enable('res.partner')
        self.record(chat, ('res.partner', gone.id))
        gone.unlink()
        task = self.task.sudo()
        for user in (self.staff, self.both, self.admin):
            self.assertFalse(task._woow_project_ai_user_allowed(user, 'internal'), user.login)
        chat2 = self.open_tab(self.staff, 'customer')
        self.record(chat2, ('x.unknown.model', 1))
        self.env.registry.clear_cache()
        self.assertIn('x.unknown.model', consult_state(self.env).ledger_other_models)
        for user in (self.staff, self.customer, self.admin):
            self.assertFalse(task._woow_project_ai_user_allowed(user, 'customer'), user.login)
        assert_consult_lockstep(self, chat | chat2, [self.staff, self.both, self.customer, self.admin])

    def test_links_of_a_disabled_kind_do_not_gate(self):
        chat = self.chat_with(self.staff, self.both)
        profile = self.enable('res.partner')
        profile.enabled = False  # used, but the partner links do not gate
        self.link(self.task, partner_id=self.partner_b.id)
        task = self.task.sudo()
        self.assertTrue(task._woow_project_ai_user_allowed(self.staff, 'internal'))
        assert_consult_lockstep(self, chat, [self.staff, self.both])
        profile.enabled = True  # at once (this transaction)
        self.assertFalse(task._woow_project_ai_user_allowed(self.staff, 'internal'))
        assert_consult_lockstep(self, chat, [self.staff, self.both])
        # the customer chat: res.partner has no customer fields, its links never gate it
        self.assertTrue(task._woow_project_ai_user_allowed(self.customer, 'customer'))

    def test_ledger_rows_survive_unlinking_and_disabling(self):
        chat = self.chat_with(self.staff, self.both)
        profile = self.enable('res.partner')
        self.link(self.task, partner_id=self.partner_b.id)
        self.record(chat, ('res.partner', self.partner_b.id))
        self.link(self.task, partner_id=False)
        profile.enabled = False
        task = self.task.sudo()
        self.assertFalse(task._woow_project_ai_user_allowed(self.staff, 'internal'), 'the ledger still gates')
        assert_consult_lockstep(self, chat, [self.staff, self.both, self.admin])

    def test_unused_rule_is_unchanged(self):
        self.chat_with(self.staff)
        self.assertEqual(self.env['discuss.channel']._woow_project_ai_consult_rule_domain(
            self.staff, [self.env.company.id]), [])
        self.env['discuss.channel']._woow_project_ai_forget_readable()
        rule = self.env['res.users'].sudo().browse(self.admin.id).woow_project_ai_readable_rule
        self.assertNotIn('woow_project_ai_consult_sent', rule.get('code') or '')

    def test_rule_terms_are_uncorrelated(self):
        chat = self.chat_with(self.staff)
        self.enable('res.partner')
        self.enable('project.tags')
        self.record(chat, ('res.partner', self.partner_a.id))
        self.link(self.task, partner_id=self.partner_a.id, tag_ids=[Command.link(self.open_tag.id)])
        for user in (self.staff, self.admin):
            code = self.rule_sql(user)
            self.assertIn('woow_project_ai_consult_sent', code)
            self.assertNotIn('EXISTS', code.upper(), 'a correlated sub-query in the consultant terms')
        self.env['discuss.channel']._woow_project_ai_forget_readable()
        rule = self.env['res.users'].sudo().browse(self.admin.id).woow_project_ai_readable_rule
        self.assertIn('woow_project_ai_consult_sent', rule['code'])
        self.assertIn(['project.task', 'partner_id'], rule['flush'])
        # a member list or a point check reads the ledger rows and links of its own chats only
        Channel = self.env['discuss.channel']
        domain = Channel._woow_project_ai_consult_rule_domain(self.staff, [self.env.company.id],
                                                              restrict_to=[chat.id])
        code = Channel.sudo()._search(domain).select().code
        self.assertIn('channel_id = ANY', code)
        self.assertNotIn('EXISTS', code.upper())

    def test_a_null_in_a_readability_source_never_opens_a_chat(self):
        """A readability source whose answer holds a NULL (an outer join): `x NOT IN R` would be NULL and open the
        chat; the rule's NOT COALESCE(x IN R, FALSE) and the predicate's x IN R keep it closed."""
        def with_null(model, user, tab, candidates):
            if not user or not user._is_internal():
                return None
            return SQL("(SELECT NULL::int UNION ALL SELECT id FROM project_tags WHERE name->>'en_US' != %s)",
                       SECRET_TAG)

        chat = self.chat_with(self.staff, self.both)
        self.post(chat, self.staff, 'question')
        task2 = self.env['project.task'].create({'name': 'Second', 'project_id': self.project.id,
                                                 'user_ids': [Command.set([self.staff.id, self.both.id])]})
        chat2 = self.open_tab(self.staff, 'internal', task=task2)
        self.open_tab(self.both, 'internal', task=task2)
        self.post(chat2, self.staff, 'question two')
        with standin(self, tag_readable=with_null):
            self.enable('project.tags')
            self.record(chat, ('project.tags', self.secret_tag.id))
            self.record(chat2, ('project.tags', self.open_tag.id))
            self.assertFalse(self.task.sudo()._woow_project_ai_user_allowed(self.staff, 'internal'))
            self.assertTrue(task2.sudo()._woow_project_ai_user_allowed(self.staff, 'internal'))
            assert_consult_lockstep(self, chat | chat2, [self.staff, self.both, self.admin])
            # a link too
            self.link(task2, tag_ids=[Command.link(self.secret_tag.id)])
            self.assertFalse(task2.sudo()._woow_project_ai_user_allowed(self.staff, 'internal'))
            assert_consult_lockstep(self, chat2, [self.staff, self.both, self.admin])

    def test_a_failing_readability_source_fails_closed_and_logs_once(self):
        chat = self.chat_with(self.staff, self.both)
        self.post(chat, self.staff, 'question')
        self.enable('project.tags')
        self.record(chat, ('project.tags', self.open_tag.id))
        task = self.task.sudo()
        self.assertTrue(task._woow_project_ai_user_allowed(self.staff, 'internal'))
        secret = 'NAME-OF-A-' + 'RECORD'

        def raising(model, user, tab, candidates):
            raise AccessError(secret)

        def wrong_type(model, user, tab, candidates):
            return 'SELECT id FROM project_tags'  # neither SQL nor a Query

        for label, source in (('raises', raising), ('wrong type', wrong_type)):
            with self.subTest(label), standin(self, tag_readable=source), \
                    patch.object(consult_mixin_module, '_query_errors_logged', set()), \
                    self.assertLogs(consult_mixin_module.__name__, level='WARNING') as logs:
                self.env['discuss.channel']._woow_project_ai_forget_readable()
                self.assertFalse(task._woow_project_ai_user_allowed(self.staff, 'internal'))
                self.assertFalse(task._woow_project_ai_user_allowed(self.staff, 'internal'))
                assert_consult_lockstep(self, chat, [self.staff, self.both])
            mine = [line for line in logs.output if 'cannot be checked for user %s;' % self.staff.id in line]
            self.assertEqual(len(mine), 1, logs.output)
            self.assertNotIn(secret, '\n'.join(logs.output))

    def test_first_consultation_is_not_hidden_by_a_cached_state(self):
        # the state was read (and cached) before the model's first ledger row: the row's key comes from the
        # registry, so the asker who can read the record keeps the chat
        chat = self.chat_with(self.staff)
        self.enable('res.partner')
        consult_state(self.env)
        self.record(chat, ('res.partner', self.partner_a.id))
        self.assertTrue(self.task.sudo()._woow_project_ai_user_allowed(self.staff, 'internal'))
        assert_consult_lockstep(self, chat, [self.staff])

    def test_lockstep_matrix(self):
        users = [self.manager, self.staff, self.staff2, self.employee, self.customer, self.admin, self.both]
        internal = self.chat_with(self.staff, self.both)
        customer = self.open_tab(self.customer, 'customer')
        self.post(internal, self.staff, 'internal question')
        self.post(customer, self.customer, 'customer question')
        chats = internal | customer
        assert_consult_lockstep(self, chats, users)  # unused
        profile = self.enable('res.partner')
        self.link(self.task, partner_id=self.partner_a.id)
        assert_consult_lockstep(self, chats, users)  # a readable link
        self.link(self.task, partner_id=self.partner_b.id)
        assert_consult_lockstep(self, chats, users)  # an unreadable link
        self.record(internal, ('res.partner', self.partner_a.id))
        self.link(self.task, partner_id=False)
        assert_consult_lockstep(self, chats, users)  # a readable ledger row only
        self.record(internal, ('res.partner', self.partner_b.id))
        profile.enabled = False
        assert_consult_lockstep(self, chats, users)  # an unreadable ledger row, the kind disabled
        self.enable('project.tags')
        self.link(self.task, tag_ids=[Command.link(self.secret_tag.id)])
        assert_consult_lockstep(self, chats, users)  # a many2many link nobody may read
