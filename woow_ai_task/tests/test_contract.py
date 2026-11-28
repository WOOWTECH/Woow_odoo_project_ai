# -*- coding: utf-8 -*-
"""Contract v1.3 (docs/CONTRACT.md): the shared checks of tests/contract/harness.py, run against this
module's own code. docs/CONTRACT.md and tests/contract/ are copied byte for byte from the contract package
(its README): never edit them here.

A conversation of the contract is the internal AI chat of a task of an "all employees" project: the person
is an employee with Project rights who opens its internal tab (the task's assignee is the project manager:
the task summary of the instructions lists the assignees as data, so the person's own name is in the
identity block only, after the kind of account); the assistant is the project's internal one. Every speaker
of the internal chat is an employee: the speaker label carries the name only (``[<name>] ``, none for a name
with an '@'), as CONTRACT.md 3.14 allows (contract_speaker_kind ''). The customer chat's labels
(``[客戶 <name>] ``, ``[負責同事 <name>] ``, ``[客戶] `` without a name) are tested by the module's own suite,
and so are the rules the checks leave to it (CONTRACT.md 5): the time zone of the current time, the name-only
labels in answers, and the file fallback beyond check 15 (tests/test_contract_v13.py).
"""

import base64
import itertools

from odoo import Command
from odoo.tests.common import new_test_user, tagged

from odoo.addons.woow_ai_task.models.ai_assistant_backend import _signature_headers
from odoo.addons.woow_ai_task.tools.markdown import markdown_to_html

from .common import ProjectAiCase
from .contract.harness import ContractChecks

_people = itertools.count(1)


@tagged('post_install', '-at_install', 'woow_ai_contract')
class TestContract(ContractChecks, ProjectAiCase):
    contract_module = 'woow_ai_task'
    contract_surface_type = 'project_task'
    contract_session_prefix = 'woow-project-ai'
    contract_models = {'connection': 'woow.project.ai.connection', 'assistant': 'woow.project.ai.assistant',
                       'job': 'woow.project.ai.job'}
    contract_text_only = False
    contract_streams = True
    contract_identity_block = True
    contract_link_policy = 'any_host'
    contract_person_has_email = True
    # the values of CONTRACT.md 3.1 (check 07 compares them); the internal chat labels by name only
    contract_speaker_label = True
    contract_events = False

    def contract_speaker_kind(self, spec):
        return ''

    def contract_gate_run(self, handle):
        for job in self.Job.search([('channel_id', '=', handle['channel'].id), ('state', '=', 'pending')]):
            job._run_job(commit=False)
        self.env.invalidate_all()

    def contract_post_with_context(self, spec, context):
        _connection, _assistant, user, channel = self._contract_setup(spec)
        channel.with_user(user).sudo().with_context(**context).message_post(
            body=spec['text'], author_id=user.partner_id.id, message_type='comment',
            subtype_xmlid='mail.mt_comment')
        return self.Job.search([('channel_id', '=', channel.id)])

    # --- adapter ---

    def _contract_setup(self, spec, chat=True):
        """A connection and an assistant of `spec`, and (chat) the internal AI chat of a task of a project
        that chose it, opened by an employee with Project rights named after `spec`."""
        vals = {'name': 'Contract %s connection' % spec['backend'], 'backend_type': spec['backend'],
                'endpoint_url': spec['endpoint'], 'openai_stream': bool(spec['stream']),
                'log_payload': bool(spec['log_payload'])}
        if spec['backend'] == 'openai':
            vals['model'] = spec['model']
        if spec['key']:
            vals['api_key'] = spec['key']
        if spec['timeout'] is not None:
            vals['timeout_seconds'] = spec['timeout']
        if spec['retries'] is not None:
            vals['max_retries'] = spec['retries']
        connection = self.env['woow.project.ai.connection'].create(vals)
        assistant = self.env['woow.project.ai.assistant'].create({
            'name': 'Contract Assistant', 'connection_id': connection.id, 'system_prompt': spec['prompt']})
        if not chat:
            return connection, assistant, None, None
        user = new_test_user(self.env, 'contract_person_%s' % next(_people),
                             groups='base.group_user,project.group_project_user', name=spec['person_name'],
                             email=spec['person_email'])
        project = self.env['project.project'].create({'name': 'Contract project', 'privacy_visibility': 'employees',
                                                      'woow_project_ai_internal_assistant_id': assistant.id})
        task = self.env['project.task'].create({'name': 'Contract task', 'project_id': project.id,
                                                'user_ids': [Command.set(self.manager.ids)]})
        channel = self.open_tab(user, 'internal', task=task)
        return connection, assistant, user, channel

    @staticmethod
    def _contract_post(channel, user, text):
        # as /mail/message/post does: the poster's environment, then sudo
        return channel.with_user(user).sudo().message_post(body=text, message_type='comment',
                                                           subtype_xmlid='mail.mt_comment')

    def contract_call(self, spec):
        _connection, assistant, user, channel = self._contract_setup(spec)
        self._contract_post(channel, user, spec['text'])
        job = self.Job.search([('channel_id', '=', channel.id)], order='id desc', limit=1)
        history = channel._woow_project_ai_history(assistant, channel._woow_project_ai_context_messages(assistant))
        result = assistant._call_backend(channel, history, job=job or None)
        log = result.get('call_log')
        return dict(result, status=log.status if log else ('error' if result['error'] else 'ok'),
                    tokens=(log.prompt_tokens, log.completion_tokens, log.total_tokens) if log else (0, 0, 0))

    def contract_turn(self, spec):
        _connection, _assistant, user, channel = self._contract_setup(spec)
        if spec.get('history'):  # the earlier turn (requests.json "history")
            earlier, file = spec['history'], spec['history']['attachment']
            attachment = self.env['ir.attachment'].with_user(user).sudo().create({
                'name': file['name'], 'raw': base64.b64decode(file['base64']), 'mimetype': file['mimetype'],
                'res_model': 'mail.compose.message', 'res_id': 0})
            channel.with_user(user).sudo().message_post(body=earlier['user_text'], message_type='comment',
                                                        subtype_xmlid='mail.mt_comment',
                                                        attachment_ids=attachment.ids)
            _assistant._post_text(channel, earlier['assistant_text'])
        self._contract_post(channel, user, spec['text'])
        jobs = self.Job.search([('channel_id', '=', channel.id)], order='id')
        for job in jobs:
            job._run_job(commit=False)
        if spec.get('twice'):  # check 15: the person asks again in the same conversation, its job runs once
            self.env.invalidate_all()
            self._contract_post(channel, user, spec['text'])
            for job in self.Job.search([('channel_id', '=', channel.id), ('id', 'not in', jobs.ids)], order='id'):
                job._run_job(commit=False)
            jobs = self.Job.search([('channel_id', '=', channel.id)], order='id')
        self.env.invalidate_all()
        logs = self.env['woow.project.ai.call.log'].search([('channel_id', '=', channel.id)])
        texts = [name for name, field in logs._fields.items() if field.type in ('char', 'text', 'html')]
        messages = self.env['mail.message'].sudo().search([('model', '=', 'discuss.channel'),
                                                           ('res_id', '=', channel.id)])
        task = channel.sudo().woow_project_ai_task_id
        activities = self.env['mail.activity'].sudo().search([('res_model', '=', 'project.task'),
                                                              ('res_id', '=', task.id)])
        return {
            'call_logs': ['\n'.join(str(log[name]) for name in texts if log[name]) for log in logs],
            'job_errors': [job.error for job in jobs if job.error],
            'messages': [str(message.body) for message in messages] + [message.subject for message in messages
                                                                        if message.subject]
                        + [str(activity.note) for activity in activities],
        }

    def contract_sign(self, secret, body, timestamp):
        return _signature_headers(secret, body, timestamp)

    def contract_session_id(self, db_uuid, channel_id):
        self.env['ir.config_parameter'].sudo().set_param('database.uuid', db_uuid)
        return self.env['woow.project.ai.assistant']._session_id(self.env['discuss.channel'].browse(channel_id))

    def contract_test_connection(self, spec):
        connection, assistant, _user, _channel = self._contract_setup(spec, chat=False)
        if spec['from_assistant']:
            return assistant.action_test_connection()
        return connection.action_test_connection()

    def contract_render(self, text, link_hosts):
        return markdown_to_html(text, link_hosts=link_hosts)

    def contract_identity_text(self, name):
        return self.env['woow.project.ai.assistant']._identity_system_text({
            'user_id': self.staff.id, 'partner_id': self.staff.partner_id.id, 'name': name,
            'email': 'contract.customer@example.com', 'lang': 'en_US', 'tz': 'Asia/Taipei', 'is_portal': False,
            'company_id': self.env.company.id})

    def contract_neutralize_seed(self):
        Connection = self.env['woow.project.ai.connection']
        litellm = Connection.create({'name': 'Neutralize LiteLLM', 'backend_type': 'openai',
                                     'endpoint_url': 'https://llm.example.com/v1', 'api_key': 'neutralize-key',
                                     'model': 'neutralize-model'})
        n8n = Connection.create({'name': 'Neutralize n8n', 'backend_type': 'n8n',
                                 'endpoint_url': 'https://n8n.example.com/webhook/neutralize',
                                 'api_key': 'neutralize-secret'})
        n8n.customer_safe = True
        assistant = self.env['woow.project.ai.assistant'].create({'name': 'Neutralize Assistant',
                                                                  'connection_id': litellm.id})
        assistant.action_generate_push_key()
        self.env.flush_all()

    def contract_gate_start(self):
        spec = self.contract_spec(backend='openai')
        connection, assistant, user, channel = self._contract_setup(spec)
        self.env['mail.message'].flush_model()
        self.env.cr.execute("SELECT COALESCE(max(id), 0) FROM mail_message")
        return {'connection': connection, 'assistant': assistant, 'user': user, 'channel': channel,
                'model': spec['model'], 'baseline': self.env.cr.fetchone()[0]}

    def contract_gate_block(self, handle, blocked):
        # a LiteLLM connection without a model is not ready (readiness "blocked": the tab cannot answer)
        handle['connection'].model = False if blocked else handle['model']

    def contract_gate_post(self, handle, text):
        self._contract_post(handle['channel'], handle['user'], text)

    def contract_gate_state(self, handle):
        channel = handle['channel']
        notices = self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', channel.id), ('id', '>', handle['baseline']),
            ('author_id', '=', handle['assistant'].partner_id.id)], order='id')
        return {'jobs': self.Job.search_count([('channel_id', '=', channel.id)]),
                'notices': notices.mapped('message_type')}

    # --- the fifteen checks (Odoo collects only the test methods a class defines itself) ---

    def test_contract_01_answers(self):
        self.contract_check_01_answers()

    def test_contract_02_requests(self):
        self.contract_check_02_requests()

    def test_contract_03_vectors(self):
        self.contract_check_03_vectors()

    def test_contract_04_render(self):
        self.contract_check_04_render()

    def test_contract_05_glossary(self):
        self.contract_check_05_glossary()

    def test_contract_06_neutralize(self):
        self.contract_check_06_neutralize()

    def test_contract_07_sums(self):
        self.contract_check_07_sums()

    def test_contract_08_secrets(self):
        self.contract_check_08_secrets()

    def test_contract_09_customer_safe(self):
        self.contract_check_09_customer_safe()

    def test_contract_10_retries(self):
        self.contract_check_10_retries()

    def test_contract_11_gate(self):
        self.contract_check_11_gate()

    def test_contract_12_identity_block(self):
        self.contract_check_12_identity_block()

    def test_contract_13_connections(self):
        self.contract_check_13_connections()

    def test_contract_14_job_rows(self):
        self.contract_check_14_job_rows()

    def test_contract_15_file_fallback(self):
        self.contract_check_15_file_fallback()
