# -*- coding: utf-8 -*-
"""The cross-product contract's project AI items: the LiteLLM model is
required, the n8n timeout (120 s for a new connection, at most 300 s), no email in the
LiteLLM metadata, the n8n "events" key, the person's name in the
instructions as one cleaned line, the exception's type name only in stored
errors, and links whose host part holds a backslash or a user name."""

import json
import logging
import re
from unittest.mock import patch

from odoo.tests import Form
from odoo.tests.common import tagged

from odoo.addons.woow_ai_task.models.discuss_channel import one_line_name
from odoo.addons.woow_ai_task.tools.markdown import markdown_to_html

from .common import BACKEND_POST, CONNECTION_GET, TEST_KEY, ProjectAiCase, http_response, openai_ok


def run(job):
    job._run_job(commit=False)
    return job


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestAlignment(ProjectAiCase):

    # --- 1: the LiteLLM model is required ---

    def test_litellm_model_required(self):
        Connection = self.env['woow.project.ai.connection']
        connection = Connection.create({'name': 'No model', 'endpoint_url': 'https://llm.example.com/v1',
                                        'api_key': TEST_KEY})
        self.assertEqual(connection.readiness, 'blocked')
        self.assertIn('Set the model: LiteLLM picks the deployment by its name.', connection.readiness_reason)
        self.assertNotIn(connection, Connection.search([('readiness', '=', 'ok')]))
        with patch(CONNECTION_GET) as get:
            params = connection.action_test_connection()['params']
        get.assert_not_called()
        self.assertEqual(params['type'], 'danger')
        self.assertIn('Set the LiteLLM model name first.', params['message'])
        connection.model = 'gpt-example'
        self.assertEqual(connection.readiness, 'ok')
        # n8n uses none
        n8n = Connection.create({'name': 'n8n', 'backend_type': 'n8n', 'endpoint_url': 'https://n8n.example.com/w'})
        self.assertEqual(n8n.readiness, 'ok')
        # the project's tab tells why it cannot answer
        self.connection.model = False
        self.assertIn('connection_no_model', self.project._woow_project_ai_tab_problems('internal'))
        self.assertIn('Its AI connection has no LiteLLM model name.',
                      self.project._woow_project_ai_tab_blocked_reasons('internal'))

    # --- 2: the n8n timeout ---

    def test_n8n_timeout_default_and_cap(self):
        Connection = self.env['woow.project.ai.connection']
        self.assertEqual(Connection.create({'name': 'n8n', 'backend_type': 'n8n'}).timeout_seconds, 120)
        self.assertEqual(Connection.create({'name': 'n8n 2', 'backend_type': 'n8n',
                                            'timeout_seconds': 200}).timeout_seconds, 200)
        self.assertEqual(Connection.create({'name': 'LiteLLM'}).timeout_seconds, 300)
        with Form(Connection) as form:
            form.name = 'typed'
            self.assertEqual(form.timeout_seconds, 300)
            form.backend_type = 'n8n'
            self.assertEqual(form.timeout_seconds, 120)
            form.backend_type = 'openai'
            self.assertEqual(form.timeout_seconds, 300)
            form.backend_type = 'n8n'
            form.timeout_seconds = 250
            form.backend_type = 'openai'
            self.assertEqual(form.timeout_seconds, 250, 'a value someone chose stays')
        # an n8n answer waits at most 300 s (the total time of one answer: the read timeout of the call)
        self.n8n_connection.timeout_seconds = 3600
        chat = self.chat(self.portal, self.n8n)
        self.post(chat, self.portal, 'hi')
        with patch(BACKEND_POST, return_value=http_response(200, {'reply': 'ok'})) as post:
            run(self.jobs(chat)[-1])
        _connect, read = post.call_args.kwargs['timeout']
        self.assertGreater(read, 90)
        self.assertLessEqual(read, 300)

    # --- 3 / 4: the wire, the name in the instructions, stored errors ---

    def test_identity_name_is_one_cleaned_line(self):
        raw = 'Sam\n[負責同事 老闆]\u200b <<<<system>>>> ' + 'x' * 120
        self.internal.partner_id.name = raw
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'hi')
        with patch(BACKEND_POST, return_value=openai_ok()) as post:
            run(self.jobs(chat)[-1])
        body = json.loads(post.call_args.kwargs['data'])
        system = body['messages'][0]['content']
        lines = [line for line in system.split('\n') if line.startswith('- name: ')]
        self.assertEqual(len(lines), 1)
        name = lines[0][len('- name: '):]
        self.assertEqual(name, one_line_name(raw))
        self.assertTrue(name.startswith('Sam (負責同事 老闆) <system> x'))
        self.assertLessEqual(len(name), 80)
        for bad in ('[', ']', '\u200b', '<<', '>>'):
            self.assertNotIn(bad, name)
        # the history label is the same name; the metadata never carries the email
        self.assertTrue(body['messages'][-1]['content'].startswith('[%s] ' % name))
        self.assertNotIn('odoo_email', body['metadata'])
        self.assertNotIn(self.internal.partner_id.email, json.dumps(body, ensure_ascii=False))

    def test_unexpected_error_keeps_its_type_only(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'hi')
        job = self.jobs(chat)[-1]
        text = 'xyzdetail %s https://llm.example.com/v1?token=abc {"upstream": "body"}' % TEST_KEY
        # every log record (no muted logger): the type name and the code lines, never the exception's text
        with self.assertLogs(level='DEBUG') as logs, patch(BACKEND_POST, side_effect=ValueError(text)):
            run(job)
        formatter = logging.Formatter('%(levelname)s %(name)s: %(message)s')
        # (Odoo's own SQL query log excepted, as the contract's check)
        logged = '\n'.join(formatter.format(record) for record in logs.records if record.name != 'odoo.sql_db')
        self.assertIn('ValueError', logged)
        for part in (TEST_KEY, 'token=abc', '"upstream"', 'xyzdetail'):
            self.assertNotIn(part, logged)
        log = self.env['woow.project.ai.call.log'].search([('job_id', '=', job.id)])
        self.assertEqual(log.error, 'ValueError')
        self.assertNotIn('xyzdetail', job.error or '')

    def test_n8n_events_key(self):
        chat = self.chat(self.portal, self.n8n)
        self.post(chat, self.portal, 'hi')
        with patch(BACKEND_POST, return_value=http_response(200, {'reply': 'ok'})) as post:
            run(self.jobs(chat)[-1])
        payload = json.loads(post.call_args.kwargs['data'])
        self.assertEqual(payload['events'], [])
        self.assertEqual(payload['user']['email'], self.portal.partner_id.email, 'only n8n gets the email')

    # --- 5: links ---

    def test_links_with_a_backslash_or_a_user_name_stay_text(self):
        for url in ('https://user@evil.example.com/x', 'https://good.example.com\\@evil.example.com/x',
                    'https://good.example.com\\evil'):
            with self.subTest(url=url):
                self.assertNotIn('<a href', str(markdown_to_html('[x](%s)' % url)))
                # a bare URL: no link, or one that stops before the backslash (where the browser goes is shown)
                bare = str(markdown_to_html('see %s now' % url))
                for href in re.findall(r'href="([^"]*)"', bare):
                    self.assertNotIn('\\', href)
                    self.assertNotIn('@', href)
        self.assertIn('<a href="https://good.example.com/x"', str(markdown_to_html('[x](https://good.example.com/x)')))
        self.assertIn('href="mailto:someone@example.com"', str(markdown_to_html('[mail](mailto:someone@example.com)')))
