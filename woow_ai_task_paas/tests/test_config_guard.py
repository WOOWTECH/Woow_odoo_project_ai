# -*- coding: utf-8 -*-
"""On the platform only project managers and administrators set a project's
AI assistants and texts. Over real RPC (the superuser mode of a plain test
would bypass the guard): U (a project user who may write the project, as
the platform lets every employee) cannot change, clear or default any of
them, not even through values the ORM stores otherwise than they read, yet
renames and copies the project (the copy has no AI settings); M and A
configure, and their copies keep the settings. Users who may not write the
project, or these fields, get Odoo's own error first."""

from lxml import etree

from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_ai_task.tests.common import TEST_KEY, make_assistant
from odoo.addons.woow_ai_task_paas.models.project_project import _value_key

from .common import PaasBridgeFixtures, RpcMixin

DENIED = 'Only project managers can set the AI assistants and the project texts of a project.'
CONFIG = ('woow_project_ai_internal_assistant_id', 'woow_project_ai_customer_assistant_id',
          'woow_project_ai_internal_brief', 'woow_project_ai_customer_brief', 'woow_project_ai_customer_welcome',
          'woow_project_ai_pause_minutes')


@tagged('post_install', '-at_install')
class TestConfigGuard(PaasBridgeFixtures, RpcMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_paas()
        cls.other_ai = make_assistant(
            cls.env, name='Another Helper', backend_type='openai', endpoint_url='https://llm-other.example.com/v1',
            api_key=TEST_KEY, model='other-model', openai_stream=False)
        texts = {'woow_project_ai_internal_brief': 'internal text', 'woow_project_ai_customer_brief': 'customer text',
                 'woow_project_ai_customer_welcome': 'welcome', 'woow_project_ai_pause_minutes': 15}
        cls.project.sudo().write(texts)
        # copies: an unbound configured project (the platform lets only a workspace owner or admin copy a bound
        # one: its binding guard refuses the copy's binding to everybody else, project managers included)
        cls.unbound = cls.env['project.project'].sudo().create(dict(
            texts, name='Unbound configured', privacy_visibility='portal',
            woow_project_ai_internal_assistant_id=cls.internal_ai.id,
            woow_project_ai_customer_assistant_id=cls.customer_ai.id))
        # an unbound project is read by its followers and its creator (the platform's rule)
        cls.unbound.message_subscribe(partner_ids=(cls.user_u | cls.user_a | cls.user_b).partner_id.ids)

    def settings(self, project):
        project = project.sudo()
        project.invalidate_recordset()
        return {name: (project[name].id if name.endswith('_id') else project[name]) for name in CONFIG}

    def assert_denied(self, result):
        self.assertIn('AccessError', self.error_of(result), result)
        self.assertIn(DENIED, self.error_of(result), result)

    def test_project_user_cannot_change_any_setting(self):
        before = self.settings(self.project)
        self.authenticate(self.user_u.login, self.user_u.login)
        for name, value in (('woow_project_ai_internal_assistant_id', self.other_ai.id),
                            ('woow_project_ai_customer_assistant_id', False),
                            ('woow_project_ai_internal_brief', 'changed'),
                            ('woow_project_ai_customer_brief', False),
                            ('woow_project_ai_customer_welcome', ''),
                            ('woow_project_ai_pause_minutes', 30)):
            self.assert_denied(self.call_kw('project.project', 'write', [self.project.ids, {name: value}]))
        self.assertEqual(self.settings(self.project), before)

    def test_project_user_cannot_change_a_setting_through_odd_values(self):
        """The ORM stores a digit string, a float or True as no record in a many2one, and 0, 0.0, [] or {} as
        their text in a text field: the guard compares what Odoo would store."""
        project = self.project.sudo()
        project.write({'woow_project_ai_customer_welcome': False})
        before = self.settings(self.project)
        assistant = project.woow_project_ai_customer_assistant_id.id
        self.authenticate(self.user_u.login, self.user_u.login)
        for name, value in (('woow_project_ai_customer_assistant_id', str(assistant)),
                            ('woow_project_ai_customer_assistant_id', float(assistant)),
                            ('woow_project_ai_customer_assistant_id', True),
                            ('woow_project_ai_customer_welcome', 0),
                            ('woow_project_ai_customer_welcome', 0.0),
                            ('woow_project_ai_customer_welcome', []),
                            ('woow_project_ai_customer_welcome', {})):
            self.assert_denied(self.call_kw('project.project', 'write', [self.project.ids, {name: value}]))
        self.assertEqual(self.settings(self.project), before)
        for value in (0, [], {}):
            self.assert_denied(self.call_kw('project.project', 'create', [
                {'name': 'U odd', 'woow_project_ai_customer_welcome': value}]))
            self.assert_denied(self.call_kw('project.project', 'create', [{'name': 'U odd default'}], {
                'context': {'default_woow_project_ai_customer_welcome': value}}))

    def test_the_comparison_reads_values_as_odoo_stores_them(self):
        """For any value, the guard's reading is the ORM's own (convert_to_cache), or unknown (a change)."""
        Project = self.env['project.project']
        samples = (None, False, 0, 0.0, 1, 7.0, True, '', 'text', '7', '10', [], {}, (), b'bytes', self.internal_ai,
                   self.internal_ai.browse())
        for name in CONFIG:
            field = Project._fields[name]
            for value in samples:
                key = _value_key(field, value)
                if key is None:
                    continue
                stored = field.convert_to_cache(value, self.project)
                self.assertEqual(key, _value_key(field, stored), (name, value))

    def test_the_guard_says_nothing_about_projects_or_fields_out_of_reach(self):
        """Odoo's own access errors come first: the same answer whether a guessed value matches or not."""
        assistant = self.project.sudo().woow_project_ai_customer_assistant_id.id
        for user in (self.user_b, self.user_e, self.user_c):
            self.authenticate(user.login, user.login)
            answers = set()
            for guess in (assistant, assistant + 1000, False):
                result = self.call_kw('project.project', 'write', [
                    self.project.ids, {'woow_project_ai_customer_assistant_id': guess}])
                self.assertIn('AccessError', self.error_of(result), (user.login, result))
                self.assertNotIn(DENIED, self.error_of(result), user.login)
                answers.add(self.error_of(result))
            self.assertEqual(len(answers), 1, (user.login, answers))

    def test_project_user_renames_and_resends_unchanged_values(self):
        self.authenticate(self.user_u.login, self.user_u.login)
        same = {name: value for name, value in self.settings(self.project).items()}
        result = self.call_kw('project.project', 'write', [self.project.ids, dict(same, name='Renamed by U')])
        self.assertTrue(result.get('result'), result)
        self.assertEqual(self.project.sudo().name, 'Renamed by U')

    def test_project_user_cannot_default_a_setting(self):
        self.authenticate(self.user_u.login, self.user_u.login)
        plain = self.call_kw('project.project', 'create', [{'name': 'U plain'}])
        self.assertTrue(plain.get('result'), plain)
        for name, value in (('woow_project_ai_internal_assistant_id', self.internal_ai.id),
                            ('woow_project_ai_customer_brief', 'text'),
                            ('woow_project_ai_pause_minutes', 5)):
            self.assert_denied(self.call_kw('project.project', 'create', [{'name': 'U default'}],
                                            {'context': {'default_%s' % name: value}}))
            self.assert_denied(self.call_kw('project.project', 'create', [{'name': 'U explicit', name: value}]))
        # the defaults themselves pass (the web client sends them)
        ok = self.call_kw('project.project', 'create', [{'name': 'U defaults', 'woow_project_ai_pause_minutes': 10,
                                                         'woow_project_ai_internal_assistant_id': False}])
        self.assertTrue(ok.get('result'), ok)

    def test_project_user_copy_drops_the_settings(self):
        self.authenticate(self.user_u.login, self.user_u.login)
        result = self.call_kw('project.project', 'copy', [self.unbound.ids])
        self.assertTrue(result.get('result'), result)
        copy = self.env['project.project'].browse(result['result'][0])
        self.assertEqual(self.settings(copy), {
            'woow_project_ai_internal_assistant_id': False, 'woow_project_ai_customer_assistant_id': False,
            'woow_project_ai_internal_brief': False, 'woow_project_ai_customer_brief': False,
            'woow_project_ai_customer_welcome': False, 'woow_project_ai_pause_minutes': 10})

    def test_an_employee_without_project_rights_copies_without_the_settings(self):
        """The platform lets every employee duplicate a project they read, but they may not even read the AI
        fields (Project users only): their copy must neither read nor carry them."""
        self.authenticate(self.user_b.login, self.user_b.login)
        result = self.call_kw('project.project', 'copy', [self.unbound.ids])
        self.assertTrue(result.get('result'), result)
        copy = self.env['project.project'].browse(result['result'][0])
        self.assertEqual(self.settings(copy), {
            'woow_project_ai_internal_assistant_id': False, 'woow_project_ai_customer_assistant_id': False,
            'woow_project_ai_internal_brief': False, 'woow_project_ai_customer_brief': False,
            'woow_project_ai_customer_welcome': False, 'woow_project_ai_pause_minutes': 10})

    def test_managers_and_administrators_configure_and_copy(self):
        for user in (self.user_m, self.user_a):
            self.authenticate(user.login, user.login)
            result = self.call_kw('project.project', 'write', [self.project.ids, {
                'woow_project_ai_internal_assistant_id': self.other_ai.id,
                'woow_project_ai_customer_brief': 'by %s' % user.login}])
            self.assertTrue(result.get('result'), result)
            self.assertEqual(self.project.sudo().woow_project_ai_internal_assistant_id, self.other_ai)
            self.project.sudo().woow_project_ai_internal_assistant_id = self.internal_ai
            copied = self.call_kw('project.project', 'copy', [self.unbound.ids])
            self.assertTrue(copied.get('result'), copied)
            copy = self.env['project.project'].browse(copied['result'][0])
            self.assertEqual(copy.sudo().woow_project_ai_internal_assistant_id, self.internal_ai)
            self.assertEqual(copy.sudo().woow_project_ai_customer_brief, 'customer text')
            self.assertEqual(copy.sudo().woow_project_ai_pause_minutes, 15)

    def test_the_form_shows_the_settings_read_only_to_project_users(self):
        """In code (_get_view), never in a stored view: the user's right is part of the view cache key, so each
        user gets the form of their own right whoever asked first."""
        for user, can in ((self.user_m, True), (self.user_u, False), (self.user_a, True), (self.user_u, False)):
            self.authenticate(user.login, user.login)
            views = self.call_kw('project.project', 'get_views', [], {'views': [[False, 'form']]})['result']
            arch = etree.fromstring(views['views']['form']['arch'])
            for name in CONFIG:
                [field] = arch.xpath("//page[@name='woow_project_ai_assistants']//field[@name='%s']" % name)
                self.assertEqual(field.get('readonly'), None if can else '1', (user.login, name))

    def test_view_cache_key_carries_the_users_right(self):
        Project = self.env['project.project']
        keys = {user: Project.with_user(user)._get_view_cache_key(view_type='form')
                for user in (self.user_u, self.user_m, self.user_a)}
        self.assertEqual(keys[self.user_m], keys[self.user_a])
        self.assertEqual([(a, b) for a, b in zip(keys[self.user_u], keys[self.user_m]) if a != b], [(False, True)])

    def test_a_generator_create_by_a_project_user_creates_the_project(self):
        """create() reads its values twice (the guard, then the runtime flag): a one-shot iterable must still
        create every project."""
        Project = self.env['project.project'].with_user(self.user_u)
        projects = Project.create(vals for vals in [{'name': 'U generator 1'}, {'name': 'U generator 2'}])
        self.assertEqual(projects.mapped('name'), ['U generator 1', 'U generator 2'])
        self.assertFalse(any(projects.sudo().mapped('use_agent_runtime')))
        with self.assertRaisesRegex(Exception, DENIED):
            Project.create(vals for vals in [{'name': 'U generator 3', 'woow_project_ai_customer_brief': 'x'}])
