# -*- coding: utf-8 -*-
"""The platform contract: every woow_paas_platform symbol, URL, model name,
xmlid, data flag and template anchor this bridge's code uses (and only
those: a symbol only the suite's fixtures use is not pinned here). A platform
release that removes or renames one of them needs a release of this module
first.
It runs with this module's suite on a database where both modules are
installed (no CI of the platform installs this module: run the suite on
every new platform version before it serves a database that has this
module).

The /woow template anchors are read from this module's own extension files
(static/src/paas/*_patch.xml), so the test checks exactly what the browser
will apply: each xpath must match exactly once in the platform's template
(Odoo applies JS template extensions in the browser, where a missing anchor
breaks the page at its first render; ``-u`` never sees it).
"""

import re

from lxml import etree

from odoo.modules.module import get_module_path
from odoo.tests.common import HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools import file_open

from odoo.addons.woow_ai_task_paas.controllers import legacy as legacy_controllers
from odoo.addons.woow_ai_task_paas.models.legacy import LEGACY_CRONS, LEGACY_ROUTES, legacy_endpoint

MODELS = (
    'woow_paas_platform.workspace_access',
    'woow_paas_platform.cloud_service',
    'woow_paas_platform.agent_session',
)
FIELDS = {
    'project.task': ('chat_enabled', 'channel_id'),
    'project.project': ('use_agent_runtime', 'cloud_service_ids', 'cloud_service_id'),
    'woow_paas_platform.agent_session': ('state', 'project_namespace_hash', 'project_id'),
    'woow_paas_platform.workspace_access': ('user_id', 'workspace_id'),
    'woow_paas_platform.cloud_service': ('project_ids', 'workspace_id'),
}
METHODS = {
    'project.task': ('_create_chat_channel',),
    'woow_paas_platform.agent_session': ('_teardown_runtime',),
}
# backend form targets changed in code (models/project_task.py, models/project_project.py)
TASK_FORM_TARGETS = ("//group[field[@name='chat_enabled']]", "//group[field[@name='channel_id']]",
                     "//page[@name='ai_chat']")
PROJECT_FORM_TARGETS = ("//field[@name='use_agent_runtime']",)
# the platform's /woow templates and this module's extensions of them
TEMPLATES = {
    'woow_paas_platform.TaskDetailPage': ('static/src/paas/pages/task-detail/TaskDetailPage.xml',
                                          'static/src/paas/task_detail_patch.xml'),
    'woow_paas_platform.ProjectKanbanPage': ('static/src/paas/pages/project-kanban/ProjectKanbanPage.xml',
                                             'static/src/paas/kanban_patch.xml'),
}
JS_EXPORTS = {
    'static/src/paas/pages/task-detail/TaskDetailPage.js': ('TaskDetailPage', 'loadTask'),
    'static/src/paas/pages/project-kanban/ProjectKanbanPage.js': ('ProjectKanbanPage', '_loadStatuses'),
}
# what task_detail_patch.{js,xml} use of the platform's page besides its class and loadTask: the component it
# renders the new tab's icon with, the tab switch, the props and state it reads, the tab classes
TASK_PAGE_JS = 'static/src/paas/pages/task-detail/TaskDetailPage.js'
TASK_PAGE_XML = 'static/src/paas/pages/task-detail/TaskDetailPage.xml'
TASK_PAGE_USES = {
    'WoowIcon in static components': r'static\s+components\s*=\s*\{[^}]*\bWoowIcon\b',
    'taskId in static props': r'static\s+props\s*=\s*\{[^}]*\btaskId\b',
    'setTab method': r'\n\s+setTab\s*\(',
    'state.activeTab': r'\bactiveTab\s*:',
    'state.task': r'\btask\s*:',
}
TASK_PAGE_CLASSES = ('o_woow_td_tab', 'o_woow_td_tab--active')
HASCLASS = re.compile(r"hasclass\(\s*'([^']+)'\s*\)")


def lxml_xpath(expr):
    """Odoo's hasclass() (a server-side and browser-side extension) in plain XPath 1.0."""
    return HASCLASS.sub(lambda m: "contains(concat(' ', normalize-space(@class), ' '), ' %s ')" % m.group(1), expr)


def platform_source(path):
    with file_open('%s/%s' % (get_module_path('woow_paas_platform'), path)) as handle:
        return handle.read()


def assert_matches_once(test, root, expr, label):
    test.assertEqual(len(root.xpath(lxml_xpath(expr))), 1, '%s: %s' % (label, expr))


def assert_source_has(test, source, pattern, label):
    test.assertRegex(source, pattern, label)


@tagged('post_install', '-at_install')
class TestPlatformContract(TransactionCase):

    def test_models(self):
        for model in MODELS:
            self.assertIn(model, self.env, model)

    def test_fields_and_methods(self):
        for model, names in FIELDS.items():
            for name in names:
                self.assertIn(name, self.env[model]._fields, '%s.%s' % (model, name))
        for model, names in METHODS.items():
            for name in names:
                self.assertTrue(callable(getattr(type(self.env[model]), name, None)), '%s.%s' % (model, name))

    def test_controllers(self):
        from odoo.addons.woow_paas_platform.controllers.ai_assistant import AiAssistantController  # noqa: PLC0415
        from odoo.addons.woow_paas_platform.controllers.portal import PaasProjectCustomerPortal  # noqa: PLC0415
        from odoo.addons.woow_ai_task.controllers.portal import WoowProjectAiPortal  # noqa: PLC0415
        self.assertTrue(callable(getattr(AiAssistantController, '_check_channel_access', None)))
        self.assertTrue(callable(getattr(PaasProjectCustomerPortal, '_paas_agent_values_for_task', None)))
        self.assertTrue(callable(getattr(PaasProjectCustomerPortal, '_project_get_page_view_values', None)))
        self.assertTrue(callable(getattr(WoowProjectAiPortal, '_woow_project_ai_open_token', None)))
        # the task-chat guard loaded (else the switch fails closed)
        self.assertTrue(legacy_controllers.TASK_CHAT_GUARD_LOADED)

    def test_routes(self):
        routing = list(self.env['ir.http'].routing_map().iter_rules())
        rules = {rule.rule for rule in routing}
        self.assertLessEqual(LEGACY_ROUTES, rules)
        # a new old-AI route must come with a bridge release
        self.assertEqual({rule for rule in rules if rule.startswith('/api/ai/')}, set(LEGACY_ROUTES))
        # the 8 rules are the 8 old methods, and no other URL is bound to one of them (it would be refused by its
        # endpoint, but the bridge would not know it)
        by_endpoint = {rule.rule for rule in routing if legacy_endpoint(rule.endpoint)}
        self.assertEqual(by_endpoint, set(LEGACY_ROUTES))
        types = {rule.rule: rule.endpoint.routing.get('type') for rule in routing if rule.rule in LEGACY_ROUTES}
        self.assertEqual({rule for rule, kind in types.items() if kind == 'http'},
                         {'/api/ai/chat/upload', '/api/ai/stream/<int:channel_id>'})
        self.assertIn('/woow', rules)
        self.assertIn('/my/tasks/<int:task_id>/woow_project_ai', rules)

    def test_woow_bundle(self):
        """The platform's /woow bundle exists under its name and carries this module's files."""
        paths = [path for path, *_rest in self.env['ir.asset']._get_asset_paths('woow_paas_platform.assets_paas', {})]
        self.assertTrue(any('woow_paas_platform/static/src/paas/pages/task-detail/TaskDetailPage.xml' in path
                            for path in paths))
        for name in ('legacy_flag.js', 'task_detail_patch.js', 'task_detail_patch.xml', 'kanban_patch.js',
                     'kanban_patch.xml', 'task_chat/WoowProjectAiPaasTaskChat.js'):
            self.assertTrue(any(path.endswith('woow_ai_task_paas/static/src/paas/%s' % name) for path in paths), name)

    def test_crons(self):
        for xmlid in LEGACY_CRONS:
            cron = self.env.ref(xmlid, raise_if_not_found=False)
            self.assertTrue(cron, xmlid)
            self.assertEqual(cron._name, 'ir.cron', xmlid)
            # noupdate: a platform upgrade never re-activates a parked cron
            module, name = xmlid.split('.')
            data = self.env['ir.model.data'].sudo().search([('module', '=', module), ('name', '=', name)])
            self.assertTrue(data.noupdate, xmlid)

    def test_backend_arch(self):
        # the switch on: the combined platform arch, untouched by this module
        self.env['ir.config_parameter'].sudo().set_param('woow_ai_task_paas.legacy_task_ai', 'True')
        task_arch = etree.fromstring(self.env['project.task'].get_view(view_type='form')['arch'])
        for xpath in TASK_FORM_TARGETS:
            self.assertEqual(len(task_arch.xpath(xpath)), 1, xpath)
        project_arch = etree.fromstring(self.env['project.project'].get_view(view_type='form')['arch'])
        for xpath in PROJECT_FORM_TARGETS:
            self.assertEqual(len(project_arch.xpath(xpath)), 1, xpath)

    def test_woow_template_anchors(self):
        platform = get_module_path('woow_paas_platform')
        for name, (template_path, patch_path) in TEMPLATES.items():
            with file_open('%s/%s' % (platform, template_path), 'rb') as handle:
                tree = etree.parse(handle)
            [template] = tree.xpath('//t[@t-name=$name]', name=name)
            root = etree.fromstring(etree.tostring(template))
            with file_open('woow_ai_task_paas/%s' % patch_path, 'rb') as handle:
                patch = etree.parse(handle)
            [extension] = patch.xpath('//t[@t-inherit=$name]', name=name)
            self.assertEqual(extension.get('t-inherit-mode'), 'extension')
            exprs = [node.get('expr') for node in extension.iter('xpath')]
            self.assertTrue(exprs, name)
            for expr in exprs:
                assert_matches_once(self, root, expr, name)

    def test_woow_js_exports(self):
        platform = get_module_path('woow_paas_platform')
        for path, (cls_name, method) in JS_EXPORTS.items():
            source = platform_source(path)
            assert_source_has(self, source, r'export\s+class\s+%s\b' % cls_name, path)
            assert_source_has(self, source, r'\n\s+(async\s+)?%s\s*\(' % re.escape(method), path)
        source = platform_source(TASK_PAGE_JS)
        for label, pattern in TASK_PAGE_USES.items():
            assert_source_has(self, source, pattern, label)
        template = platform_source(TASK_PAGE_XML)
        for css_class in TASK_PAGE_CLASSES:
            assert_source_has(self, template, r"'%s'|\b%s\b" % (re.escape(css_class), re.escape(css_class)), css_class)
        # the platform module paths the patches import
        for source_path in ('static/src/paas/services/rpc.js', 'static/src/paas/core/platform_config.js'):
            with file_open('%s/%s' % (platform, source_path)) as handle:
                self.assertTrue(handle.read())
        with file_open('%s/static/src/paas/services/rpc.js' % platform) as handle:
            self.assertRegex(handle.read(), r'export\s+async\s+function\s+jsonRpc\b')

    def test_the_checks_fail_on_a_missing_name(self):
        """The helpers above do fail when a name is gone (so a green run means the names are there)."""
        root = etree.fromstring('<div><div class="o_woow_td_tabs"/></div>')
        assert_matches_once(self, root, "//div[hasclass('o_woow_td_tabs')]", 'present')
        with self.assertRaises(AssertionError):
            assert_matches_once(self, root, "//div[hasclass('o_woow_td_tabs_renamed')]", 'missing')
        source = platform_source(TASK_PAGE_JS)
        with self.assertRaises(AssertionError):
            assert_source_has(self, source, r'\n\s+setTabRenamedForTheTest\s*\(', 'missing method')

    def test_project_ai_symbols(self):
        """The woow_ai_task symbols the bridge calls (same repository)."""
        for model, names in {
            'project.project': ('_woow_project_ai_tab_assistant',),
            'project.task': ('_woow_project_ai_user_allowed', '_woow_project_ai_tab_channel',
                             'woow_project_ai_open_tab'),
            'discuss.channel': ('_woow_project_ai_forget_readable', '_woow_project_ai_sync_partners',
                                '_woow_project_ai_sync_projects'),
        }.items():
            for name in names:
                self.assertTrue(callable(getattr(type(self.env[model]), name, None)), '%s.%s' % (model, name))


@tagged('post_install', '-at_install')
class TestPlatformContractWoowPage(HttpCase):

    def test_woow_page_carries_the_flag(self):
        portal = new_test_user(self.env, 'paasb_contract_portal', groups='base.group_portal',
                               email='paasb_contract_portal@example.com')
        self.authenticate(portal.login, portal.login)
        response = self.url_open('/woow', timeout=30)
        self.assertEqual(response.status_code, 200)
        self.assertIn('woow_project_ai_paas_legacy', response.text)
