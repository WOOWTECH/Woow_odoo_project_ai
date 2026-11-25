# -*- coding: utf-8 -*-
"""The platform's own old task chats: archived and restricted to system
administrators (their messages, members and task link kept), recorded, and
restored exactly by the undo: every top-level "Task Chat: ..." channel of
type channel, shared by several tasks or pointed to by none (threads are
left out), each in its own savepoint. A channel a task points to that is not
one of them is left alone and logged. The assertions look at this test's own
chats only: a database may hold other old chats (restricted already, or not
yet)."""

import json
from unittest.mock import patch

from odoo.tests.common import HttpCase, tagged

from .common import RESTRICTED_PARAM, PaasBridgeFixtures, RpcMixin

RELEASED_PARAM = 'woow_ai_task_paas.released_chats'


@tagged('post_install', '-at_install')
class TestOldChannels(PaasBridgeFixtures, RpcMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_paas()
        cls.setup_legacy_seed()
        cls.Task = cls.env['project.task']
        cls.administrators = cls.env.ref('base.group_system')
        # Odoo gives a new channel of type 'channel' the group of all employees (every employee reads it)
        cls.employees = cls.env.ref('base.group_user')

    def chat_state(self, channel):
        channel = channel.sudo().with_context(active_test=False)
        channel.invalidate_recordset()
        return channel.active, channel.group_public_id

    def message_count(self, channel):
        return self.env['mail.message'].sudo().search_count([('model', '=', 'discuss.channel'),
                                                             ('res_id', '=', channel.id)])

    def record(self):
        return json.loads(self.env['ir.config_parameter'].sudo().get_param(RESTRICTED_PARAM) or '{}')

    def test_restrict_records_and_keeps_everything(self):
        messages = self.message_count(self.legacy_chat)
        members = self.members(self.legacy_chat)
        with self.assertLogs('odoo.addons.woow_ai_task_paas.models.project_task', 'WARNING') as logs:
            report = self.Task._woow_project_ai_paas_restrict_legacy_chats()
        self.assertIn(self.legacy_chat.id, report['restricted'])
        self.assertIn(self.company_channel.id, report['others'])
        self.assertNotIn(self.company_channel.id, report['restricted'])
        self.assertIn(str(self.company_channel.id), '\n'.join(logs.output))
        self.assertEqual(self.chat_state(self.legacy_chat), (False, self.administrators))
        self.assertEqual(self.record().get(str(self.legacy_chat.id)), [True, self.employees.id])
        self.assertNotIn(str(self.company_channel.id), self.record())
        # messages, members and the task's link stay
        self.assertEqual(self.message_count(self.legacy_chat), messages)
        self.assertEqual(self.members(self.legacy_chat), members)
        self.legacy_task.invalidate_recordset(['channel_id'])
        self.assertEqual(self.legacy_task.channel_id, self.legacy_chat)
        # the company channel a task points to: untouched
        self.assertEqual(self.chat_state(self.company_channel), (True, self.employees))
        # a second run changes nothing
        record = self.record()
        again = self.Task._woow_project_ai_paas_restrict_legacy_chats()
        self.assertEqual(again['restricted'], [])
        self.assertEqual(self.chat_state(self.legacy_chat), (False, self.administrators))
        self.assertEqual(self.record(), record)

    def test_only_the_platforms_own_chats(self):
        Channel = self.env['discuss.channel'].sudo()
        # a "Task Chat: ..." channel two tasks point to whose name matches neither of them now (a task renamed,
        # then copied with its channel_id): the platform's; a group chat named like one: not the platform's
        shared = Channel.create({'name': 'Task Chat: first name', 'channel_type': 'channel'})
        group_chat = Channel.create({'name': 'Task Chat: a group', 'channel_type': 'group'})
        tasks = self.env['project.task'].sudo().create([
            {'name': 'Renamed', 'project_id': self.legacy_project.id},
            {'name': 'Renamed (copy)', 'project_id': self.legacy_project.id},
            {'name': 'Group', 'project_id': self.legacy_project.id}])
        self.env.cr.execute("UPDATE project_task SET channel_id = %s WHERE id IN %s", [shared.id, tuple(tasks[:2].ids)])
        self.env.cr.execute("UPDATE project_task SET channel_id = %s WHERE id = %s", [group_chat.id, tasks[2].id])
        report = self.Task._woow_project_ai_paas_restrict_legacy_chats()
        self.assertLessEqual({self.legacy_chat.id, shared.id}, set(report['restricted']))
        self.assertLessEqual({group_chat.id, self.company_channel.id}, set(report['others']))
        self.assertNotIn(group_chat.id, report['restricted'])
        self.assertEqual(self.chat_state(shared), (False, self.administrators))
        self.assertTrue(self.chat_state(group_chat)[0])
        self.assertEqual(self.record().get(str(shared.id)), [True, self.employees.id])

    def test_employees_read_nothing_administrators_still_do(self):
        self.Task._woow_project_ai_paas_restrict_legacy_chats()
        # B was a member of the old chat
        self.assertIn(self.user_b.partner_id, self.members(self.legacy_chat))
        self.authenticate(self.user_b.login, self.user_b.login)
        result = self.json_rpc('/mail/thread/messages', {'thread_model': 'discuss.channel',
                                                         'thread_id': self.legacy_chat.id}).json()
        self.assertNotIn('legacy question', json.dumps(result))
        self.assertEqual(self.url_open('/discuss/channel/%s' % self.legacy_chat.id, timeout=30).status_code, 404)
        # a system administrator still reads it (Odoo's administrator rule on channels)
        chat = self.legacy_chat.with_user(self.user_a).with_context(active_test=False)
        self.assertEqual(chat.read(['name'])[0]['name'], 'Task Chat: Legacy chat task')
        bodies = self.env['mail.message'].with_user(self.user_a).search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', self.legacy_chat.id)]).mapped('body')
        self.assertTrue(any('legacy question' in str(body) for body in bodies))

    def test_undo_restores_exactly(self):
        # an old chat that already had a group and was archived: its own values come back
        Channel = self.env['discuss.channel'].sudo()
        access_admins = self.env.ref('base.group_erp_manager')
        other = Channel.create({'name': 'Task Chat: second', 'channel_type': 'channel', 'active': False,
                                'group_public_id': access_admins.id})
        task = self.env['project.task'].sudo().create({'name': 'Second', 'project_id': self.legacy_project.id})
        self.env.cr.execute("UPDATE project_task SET channel_id = %s WHERE id = %s", [other.id, task.id])
        report = self.Task._woow_project_ai_paas_restrict_legacy_chats()
        self.assertLessEqual({self.legacy_chat.id, other.id}, set(report['restricted']))
        self.assertEqual(self.chat_state(other), (False, self.administrators))
        restored = self.Task._woow_project_ai_paas_unrestrict_legacy_chats([other.id])
        self.assertEqual(restored, other.ids)
        self.assertEqual(self.chat_state(other), (False, access_admins))
        self.assertNotIn(str(other.id), self.record())
        self.assertIn(str(self.legacy_chat.id), self.record())
        self.Task._woow_project_ai_paas_unrestrict_legacy_chats([self.legacy_chat.id])
        self.assertEqual(self.chat_state(self.legacy_chat), (True, self.employees))
        self.assertNotIn(str(self.legacy_chat.id), self.record())

    def test_switch_on_restricts_nothing(self):
        with self.legacy_on():
            report = self.Task._woow_project_ai_paas_restrict_legacy_chats()
        self.assertEqual(report['restricted'], [])
        self.assertEqual(self.chat_state(self.legacy_chat), (True, self.employees))

    def test_an_undo_lasts_across_upgrades(self):
        """The restriction runs at every upgrade: a chat an administrator restored stays restored."""
        self.Task._woow_project_ai_paas_restrict_legacy_chats()
        self.assertEqual(self.Task._woow_project_ai_paas_unrestrict_legacy_chats([self.legacy_chat.id]),
                         self.legacy_chat.ids)
        self.assertIn(self.legacy_chat.id,
                      json.loads(self.env['ir.config_parameter'].sudo().get_param(RELEASED_PARAM)))
        again = self.Task._woow_project_ai_paas_restrict_legacy_chats()  # the next upgrade
        self.assertNotIn(self.legacy_chat.id, again['restricted'])
        self.assertEqual(self.chat_state(self.legacy_chat), (True, self.employees))

    def test_a_copied_tasks_chat_is_the_platforms_too(self):
        """The platform copies channel_id with a task: two tasks point to its "Task Chat: <name>" channel."""
        copy = self.legacy_task.sudo().copy()
        self.env.cr.execute("UPDATE project_task SET channel_id = %s WHERE id = %s", [self.legacy_chat.id, copy.id])
        copy.invalidate_recordset(['channel_id'])
        self.assertEqual(copy.channel_id, self.legacy_chat)
        report = self.Task._woow_project_ai_paas_restrict_legacy_chats()
        self.assertIn(self.legacy_chat.id, report['restricted'])
        self.assertEqual(self.chat_state(self.legacy_chat), (False, self.administrators))

    def test_an_orphan_task_chat_is_restricted_too(self):
        """The platform does not delete a task's chat with the task: such a chat is the platform's too, and the
        undo restores it."""
        orphan = self.env['discuss.channel'].sudo().create({'name': 'Task Chat: a deleted task',
                                                            'channel_type': 'channel'})
        with self.assertLogs('odoo.addons.woow_ai_task_paas.models.project_task', 'WARNING') as logs:
            report = self.Task._woow_project_ai_paas_restrict_legacy_chats()
        self.assertIn(orphan.id, report['restricted'])
        self.assertNotIn(orphan.id, report['others'])
        # the warning lists only channels that are not the platform's own task chats (the company channel)
        self.assertNotIn('[%s' % orphan.id, '\n'.join(logs.output))
        self.assertNotIn(' %s]' % orphan.id, '\n'.join(logs.output))
        self.assertEqual(self.chat_state(orphan), (False, self.administrators))
        self.assertEqual(self.record().get(str(orphan.id)), [True, self.employees.id])
        self.assertEqual(self.Task._woow_project_ai_paas_unrestrict_legacy_chats([orphan.id]), orphan.ids)
        self.assertEqual(self.chat_state(orphan), (True, self.employees))

    def test_a_thread_named_like_an_old_chat_is_left_alone(self):
        """A thread (sub-channel) named "Task Chat: ..." is not one of the platform's chats: Odoo forbids a group on
        threads (their access follows their parent's), and selecting one made the whole restriction fail. The old
        chats are still restricted and recorded; the thread is untouched."""
        thread = self.company_channel.sudo()._create_sub_channel(name='Task Chat: x')
        self.assertEqual(thread.parent_channel_id, self.company_channel)
        report = self.Task._woow_project_ai_paas_restrict_legacy_chats()
        self.assertFalse(report.get('error'), report)
        self.assertFalse(report.get('failed'), report)
        self.assertIn(self.legacy_chat.id, report['restricted'])
        self.assertNotIn(thread.id, report['restricted'])
        self.assertEqual(self.chat_state(self.legacy_chat), (False, self.administrators))
        self.assertEqual(self.record().get(str(self.legacy_chat.id)), [True, self.employees.id])
        self.assertNotIn(str(thread.id), self.record())
        self.assertEqual(self.chat_state(thread), (True, self.env['res.groups']))

    def test_a_chat_that_cannot_be_written_is_left_alone_and_unrecorded(self):
        """Each chat is recorded and restricted in its own savepoint: when its write fails after its record entry
        was written, both are rolled back (it is recorded nowhere, so the next run tries again) and the other chats
        are restricted."""
        Channel = self.env['discuss.channel'].sudo()
        other = Channel.create({'name': 'Task Chat: second', 'channel_type': 'channel'})
        task = self.env['project.task'].sudo().create({'name': 'Second', 'project_id': self.legacy_project.id})
        self.env.cr.execute("UPDATE project_task SET channel_id = %s WHERE id = %s", [other.id, task.id])
        failure = RuntimeError('secret detail')
        original = type(Channel).write
        legacy_id = self.legacy_chat.id

        def write(records, vals):
            if records.ids == [legacy_id] and 'group_public_id' in vals:
                raise failure
            return original(records, vals)

        with patch.object(type(Channel), 'write', write), \
                self.assertLogs('odoo.addons.woow_ai_task_paas.models.project_task', 'ERROR') as logs:
            report = self.Task._woow_project_ai_paas_restrict_legacy_chats()
        self.assertEqual(report.get('failed'), [legacy_id])
        self.assertIn(other.id, report['restricted'])
        self.assertNotIn(str(legacy_id), self.record())
        self.assertEqual(self.record().get(str(other.id)), [True, self.employees.id])
        self.assertEqual(self.chat_state(self.legacy_chat), (True, self.employees))
        self.assertEqual(self.chat_state(other), (False, self.administrators))
        output = '\n'.join(logs.output)
        self.assertIn('old task chat %s could not be restricted' % legacy_id, output)
        self.assertIn('RuntimeError', output)
        self.assertNotIn('secret detail', output)
        again = self.Task._woow_project_ai_paas_restrict_legacy_chats()
        self.assertIn(legacy_id, again['restricted'])
        self.assertEqual(self.record().get(str(legacy_id)), [True, self.employees.id])
