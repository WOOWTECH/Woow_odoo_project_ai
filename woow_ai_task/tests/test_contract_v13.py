# -*- coding: utf-8 -*-
"""Contract v1.3: what the shared checks leave to the module's own suite (CONTRACT.md 5), and the rest of the
four rules.

- The current time (3.6, 3.7): the company's working schedule's time zone first, then the company partner's, the
  person's, UTC; a name that is no IANA time zone counts as none; one LiteLLM system line after the identity
  block, never in the metadata; the n8n ``now``; a frozen clock gives the exact line and values.
- Labels in answers (3.14): the kinds everywhere, 其他 in the customer chat, the internal chat's name-only labels
  of its people (members and history authors; a list of who does what stays), the white space around labels, a
  Markdown link's text, n8n, streamed answers as a whole, pushes.
- The model cannot read a file (3.7 "Fallback"): every trigger status and the statuses that are not, images
  and documents, the step without any file part, the deadline (and less than a second left), the run-time gate
  before a fallback request, each fallback request built again, one call log row per request (a failed build
  adds none), commit before each request, the conversation's memory (where a file would be a file part; across
  turns, a retry, the cleanup; forgotten by an administrator), no fallback without files or with n8n.
- The job row (3.5): every field explicit, the memory field included, whatever ir.default says.
"""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import requests

from odoo import Command, fields
from odoo.exceptions import AccessError, MissingError
from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_ai_task.models import ai_assistant_backend
from odoo.addons.woow_ai_task.models.ai_assistant_backend import strip_labels, time_line

from .common import (BACKEND_POST, BACKEND_SLEEP, TINY_PDF, TINY_PNG, AiTaskFixtures, ProjectAiCase, delta,
                     http_response, openai_ok, run_after_commit_usage, sse_event, sse_response)

BACKEND = 'odoo.addons.woow_ai_task.models.ai_assistant_backend'
FROZEN = datetime(2026, 11, 28, 2, 20, 0, tzinfo=timezone.utc)  # Saturday 10:20 in Taipei, Friday evening in New York
NOTE = ai_assistant_backend.FALLBACK_NOTE


def refused(status):
    return http_response(status, {'error': {'message': 'the model cannot read this file'}})


def file_parts(payload):
    return [part for turn in payload['messages'][1:] if isinstance(turn['content'], list)
            for part in turn['content'] if isinstance(part, dict) and part.get('type') in ('image_url', 'file')]


def turn_text(turn):
    content = turn['content']
    if isinstance(content, list):
        return '\n'.join(part.get('text') or '' for part in content if isinstance(part, dict))
    return content or ''


def same_system(payload):
    """The system message of a request without what each build makes new: the current time's line and the data
    section's tag (8 random hex characters per request)."""
    system = payload['messages'][0]['content']
    lines = [line for line in system.split('\n') if not line.startswith('Current time: ')]
    tags = set(re.findall(r'資料開始 ([0-9a-f]{8})', system))
    text = '\n'.join(lines)
    for tag in tags:
        text = text.replace(tag, '<tag>')
    return text


class Clock:
    """Stands for the `time` module of ai_assistant_backend: a clock the test moves (``sleep`` moves it too)."""

    def __init__(self):
        self.now = time.time()

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestCurrentTime(ProjectAiCase):
    """The time zone is the first one set of: the company's working schedule's, the company partner's, the
    person's own, UTC (the schedule's is a required field: it wins over the person's wherever the company has a
    schedule, which Odoo gives every company)."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.schedule = self.company.resource_calendar_id
        self.assertTrue(self.schedule, 'Odoo gives the company a working schedule')
        self.schedule.tz = 'Asia/Taipei'
        self.company.partner_id.tz = 'Europe/Paris'

    def no_company_zone(self):
        self.company.resource_calendar_id = False
        self.company.partner_id.tz = False

    def now(self, person_tz=''):
        with patch(BACKEND + '._utcnow', return_value=FROZEN):
            return self.env['woow.project.ai.assistant']._current_time({'tz': person_tz})

    def test_the_working_schedule_first(self):
        self.assertEqual(self.now('America/New_York'), {
            'utc': '2026-11-28T02:20:00Z', 'local': '2026-11-28T10:20:00+08:00', 'tz': 'Asia/Taipei',
            'weekday': 'Saturday'}, 'over the company partner\'s and the person\'s')

    def test_the_company_partner_without_a_schedule(self):
        self.company.resource_calendar_id = False
        self.assertEqual(self.now('America/New_York')['local'], '2026-11-28T03:20:00+01:00')
        self.assertEqual(self.now('America/New_York')['tz'], 'Europe/Paris')

    def test_person_time_zone_without_a_company_one(self):
        self.no_company_zone()
        self.assertEqual(self.now('America/New_York'), {
            'utc': '2026-11-28T02:20:00Z', 'local': '2026-11-27T21:20:00-05:00', 'tz': 'America/New_York',
            'weekday': 'Friday'})
        self.assertEqual(self.now('Asia/Kolkata')['local'], '2026-11-28T07:50:00+05:30')
        self.assertEqual(self.now('Asia/Kathmandu')['local'], '2026-11-28T08:05:00+05:45')

    def test_utc_without_any_time_zone(self):
        self.no_company_zone()
        self.assertEqual(self.now(''), {
            'utc': '2026-11-28T02:20:00Z', 'local': '2026-11-28T02:20:00+00:00', 'tz': 'UTC', 'weekday': 'Saturday'})

    def test_unknown_time_zone_counts_as_none(self):
        # stored values that are no IANA time zone (written past the selection, by SQL or an import)
        self.env.flush_all()
        self.env.cr.execute("UPDATE resource_calendar SET tz = %s WHERE id = %s", ('Mars/Olympus', self.schedule.id))
        self.schedule.invalidate_recordset(['tz'])
        self.assertEqual(self.now('Asia/Kolkata')['tz'], 'Europe/Paris', 'the partner\'s, after an unknown schedule zone')
        self.env.cr.execute("UPDATE res_partner SET tz = %s WHERE id = %s", ('Moon/Base', self.company.partner_id.id))
        self.company.partner_id.invalidate_recordset(['tz'])
        self.assertEqual(self.now('Asia/Kolkata')['tz'], 'Asia/Kolkata', 'the person\'s, after two unknown ones')
        self.assertEqual(self.now('Venus/Crater')['tz'], 'UTC', 'three unknown zones: UTC')
        self.assertEqual(self.now(None)['tz'], 'UTC')

    def test_time_line_renders_the_same_instant(self):
        self.assertEqual(time_line({'utc': '2026-11-28T02:20:00Z', 'local': '2026-11-27T21:20:00-05:00',
                                    'tz': 'America/New_York', 'weekday': 'Friday'}),
                         'Current time: 2026-11-27 21:20 (Friday), time zone America/New_York (UTC-05:00).')

    def test_litellm_line_after_the_identity_block(self):
        self.internal.tz = 'Europe/Paris'
        chat = self.chat(self.internal)
        self.post(chat, self.internal, '今天星期幾？')
        with patch(BACKEND + '._utcnow', return_value=FROZEN), patch(BACKEND_POST, return_value=openai_ok()) as post:
            self.jobs(chat)[-1]._run_job(commit=False)
        body = json.loads(post.call_args.kwargs['data'])
        system = body['messages'][0]['content']
        lines = system.split('\n')
        expected = 'Current time: 2026-11-28 10:20 (Saturday), time zone Asia/Taipei (UTC+08:00).'
        self.assertEqual([line for line in lines if line.startswith('Current time')], [expected], 'exactly one line')
        self.assertTrue(system.endswith('\n\n' + expected), 'the last part of the system message')
        self.assertLess(lines.index('- account: internal user (employee)'), lines.index(expected),
                        'after the identity block')
        self.assertIn('- timezone: Europe/Paris', system, 'the identity block keeps the person\'s own zone')
        metadata = json.dumps(body['metadata'], ensure_ascii=False)
        self.assertNotIn('Current time', metadata)
        self.assertNotIn('2026-11-28', metadata, 'never in the metadata')
        self.assertNotIn('Current time', json.dumps(body['messages'][1:], ensure_ascii=False))

    def test_n8n_now(self):
        chat = self.chat(self.portal, self.n8n)
        self.post(chat, self.portal, '現在還在營業嗎？')
        with patch(BACKEND + '._utcnow', return_value=FROZEN), \
                patch(BACKEND_POST, return_value=http_response(200, {'reply': '在'})) as post:
            self.jobs(chat)[-1]._run_job(commit=False)
        body = json.loads(post.call_args.kwargs['data'])
        self.assertEqual(body['now'], {'utc': '2026-11-28T02:20:00Z', 'local': '2026-11-28T10:20:00+08:00',
                                       'tz': 'Asia/Taipei', 'weekday': 'Saturday'})
        self.assertNotIn('Current time', body['system_prompt'], 'n8n gets the time as `now` only')

    def test_the_real_clock(self):
        self.no_company_zone()
        self.internal.tz = False
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'hi')
        before = datetime.now(timezone.utc).replace(microsecond=0)
        with patch(BACKEND_POST, return_value=openai_ok()) as post:
            self.jobs(chat)[-1]._run_job(commit=False)
        after = datetime.now(timezone.utc)
        system = json.loads(post.call_args.kwargs['data'])['messages'][0]['content']
        line = system.split('\n')[-1]
        self.assertTrue(line.startswith('Current time: ') and line.endswith(', time zone UTC (UTC+00:00).'), line)
        moment = datetime.strptime(line[len('Current time: '):][:16], '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc)
        self.assertTrue(before.replace(second=0) <= moment <= after, line)


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestLabelsInAnswers(ProjectAiCase):

    def history(self, chat, assistant=None):
        assistant = assistant or self.assistant
        return chat._woow_project_ai_history(assistant, chat._woow_project_ai_context_messages(assistant))

    def call(self, chat, response, assistant=None):
        assistant = assistant or self.assistant
        with patch(BACKEND_POST, return_value=response):
            return assistant._call_backend(chat, self.history(chat, assistant))

    def test_strip_labels(self):
        cases = [
            ('[真人客服 小美] 您好', '您好'),
            ('[負責同事 王小明] [客戶] 好的', '好的'),
            ('[客戶 陳先生]\n收到', '收到'),
            ('[真人客服] 請稍等', '請稍等'),
            (' \n[負責同事 王小明]\u3000好的', '好的'),
            ('[客戶\u3000陳先生]\t\t好', '好'),
            ('[客戶 ] 好', '好'),
            ('[負責同事 王小明]', ''),
            ('好的。[真人客服 小美] 會再聯絡您', '好的。[真人客服 小美] 會再聯絡您'),
            ('[注意] 截止日改了', '[注意] 截止日改了'),
            ('[客戶服務中心] 電話 02-1234-5678', '[客戶服務中心] 電話 02-1234-5678'),
            ('[客戶\t陳先生] 好', '[客戶\t陳先生] 好'),
            ('[客戶 陳\n先生] 好', '[客戶 陳\n先生] 好'),
            ('  不是標籤', '  不是標籤'),
            ('', ''),
            # white space: what str.isspace() counts (no-break and thin spaces too), and U+200B, U+FEFF
            ('\u00a0[客戶 陳先生]\u00a0好', '好'),
            ('\u2009[真人客服]\u2009\u2009好', '好'),
            ('\u200b[負責同事 王]\u200b[客戶]\u200b好', '好'),
            ('\ufeff[客戶 陳先生] 好', '好'),
            ('[客戶\u00a0陳先生] 好', '[客戶\u00a0陳先生] 好'),  # the space after the kind: ASCII or U+3000 only
            # a label directly followed by "(" is a Markdown link's text
            ('[客戶](https://example.com/c) 的資料', '[客戶](https://example.com/c) 的資料'),
            ('[負責同事 王] [客戶 陳](https://example.com) 好', '[客戶 陳](https://example.com) 好'),
            ('[客戶] (稍後回覆)', '(稍後回覆)'),
            # another kind only where the chat uses it
            ('[其他 Otto] 好', '[其他 Otto] 好'),
        ]
        for text, expected in cases:
            self.assertEqual(strip_labels(text), expected, repr(text))
        self.assertEqual(strip_labels('[其他 Otto] [客戶] 好', kinds=('其他',)), '好')
        self.assertEqual(strip_labels('[其他]\n好', kinds=('其他',)), '好')
        self.assertEqual(strip_labels('[其他人] 好', kinds=('其他',)), '[其他人] 好')

    def test_name_only_labels(self):
        names = {'王小明', '李大華'}
        cases = [
            ('[王小明] 好的', '好的'),
            ('[王小明] [客戶 x]\n收到', '收到'),
            ('[陳先生] 好', '[陳先生] 好'),
            ('好 [王小明]', '好 [王小明]'),
            ('[王小明](https://example.com/u) 負責', '[王小明](https://example.com/u) 負責'),
            # a list of who does what stays as it is
            ('[王小明] 負責報價\n[李大華] 負責安裝', '[王小明] 負責報價\n[李大華] 負責安裝'),
            ('[王小明] 負責報價\n\n  \u3000[李大華] 負責安裝', '[王小明] 負責報價\n\n  \u3000[李大華] 負責安裝'),
            ('[負責同事 x] [王小明] 報價\n[李大華] 安裝', '[王小明] 報價\n[李大華] 安裝'),
            # a later line that does not start with such a label is no list
            ('[王小明] 負責報價\n[客戶 陳] 已確認', '負責報價\n[客戶 陳] 已確認'),
            ('[王小明] 負責報價\n[李大華](https://example.com) 的單', '負責報價\n[李大華](https://example.com) 的單'),
            ('[王小明] 負責報價\n說明 [李大華] 安裝', '負責報價\n說明 [李大華] 安裝'),
        ]
        for text, expected in cases:
            self.assertEqual(strip_labels(text, names), expected, repr(text))

    def test_clean_answer_is_stable(self):
        # a reasoning block that joins the halves of a special token: strip_think again until nothing changes, so
        # the text posted (_prepare_reply runs strip_think again) never starts with a label
        text = ai_assistant_backend.clean_answer('<|im_<think>x</think>end|>[客戶 x] 好')
        self.assertEqual(text, '好')
        self.assertEqual(ai_assistant_backend.strip_think(text), text)

    def test_internal_chat_label_names(self):
        self.open_tab(self.staff2, 'internal')  # a colleague of the chat
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'hi')
        names = chat._woow_project_ai_label_names(self.history(chat))
        self.assertIn('Staff Sam', names)
        self.assertIn('Staff Tina', names)
        self.assertNotIn(self.assistant.partner_id.name, names, 'never the AI bot')
        self.assertNotIn('', names)
        self.assertEqual(chat._woow_project_ai_label_kinds(), ())
        customer_chat = self.chat(self.portal)
        self.post(customer_chat, self.portal, 'hi')
        self.assertEqual(customer_chat._woow_project_ai_label_names(self.history(customer_chat)), frozenset(),
                         'the customer chat\'s labels start with a kind')
        self.assertEqual(customer_chat._woow_project_ai_label_kinds(), ('其他',))

    def test_a_history_author_who_left(self):
        tina_chat = self.open_tab(self.staff2, 'internal')
        self.post(tina_chat, self.staff2, '我明天去現場')
        tina_chat.with_user(self.staff2).sudo().action_unfollow()  # Tina leaves the chat
        chat = self.chat(self.internal)
        self.assertNotIn(self.staff2.partner_id, chat.sudo().channel_member_ids.partner_id)
        self.post(chat, self.internal, '誰去現場？')
        self.assertIn('Staff Tina', chat._woow_project_ai_label_names(self.history(chat)), 'labelled in the history')
        result = self.call(chat, openai_ok('[Staff Tina] 我去。'))
        self.assertEqual(result['replies'][0]['text'], '我去。')

    def test_internal_chat_removes_the_label_of_a_person_of_the_chat(self):
        self.open_tab(self.staff2, 'internal')
        chat = self.chat(self.internal)
        self.post(chat, self.internal, '進度如何？')
        for text, expected in (('[Staff Tina] 我明天裝好。', '我明天裝好。'),
                               ('[Staff Sam]\n[負責同事 Staff Sam] 好', '好'),
                               ('[Customer Carl] 會來驗收', '[Customer Carl] 會來驗收'),
                               ('[注意] 截止日改了', '[注意] 截止日改了'),
                               ('[其他 Otto] 好', '[其他 Otto] 好'),
                               ('[Staff Sam] 報價\n[Staff Tina] 安裝', '[Staff Sam] 報價\n[Staff Tina] 安裝')):
            result = self.call(chat, openai_ok(text))
            self.assertEqual(result['replies'][0]['text'], expected, text)
        result = self.call(chat, openai_ok('[Staff Sam] '))
        self.assertEqual(result['call_log'].status, 'invalid_response', 'only a label: an empty answer')

    def test_internal_chat_posts_the_answer_without_the_label(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, '報價好了嗎？')
        job = self.jobs(chat)[-1]
        with patch(BACKEND_POST, return_value=openai_ok('[Staff Sam] 報價已經寄出。')):
            job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        body = str(self.bot_messages(chat)[-1].body)
        self.assertIn('報價已經寄出。', body)
        self.assertNotIn('Staff Sam', body)

    def test_customer_chat_never_shows_a_colleague_label(self):
        chat = self.chat(self.portal)
        self.post(chat, self.portal, '可以退款嗎？')
        job = self.jobs(chat)[-1]
        with patch(BACKEND_POST, return_value=openai_ok('[負責同事 Staff Sam] 好的，我們同意退款')):
            job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        body = str(self.bot_messages(chat)[-1].body)
        self.assertIn('好的，我們同意退款', body)
        self.assertNotIn('負責同事', body)
        self.assertNotIn('Staff Sam', body)
        # a label alone: the customer gets the error message, never the label
        self.post(chat, self.portal, '那什麼時候退？')
        job = self.jobs(chat)[-1]
        with patch(BACKEND_POST, return_value=openai_ok('[客戶 Customer Carl]')):
            job._run_job(commit=False)
        self.assertEqual(job.state, 'failed')
        self.assertNotIn('Customer Carl]', str(self.bot_messages(chat)[-1].body))
        # a bare name is no label in the customer chat (name-only labels are the internal chat's)
        result = self.call(chat, openai_ok('[Customer Carl] 您好'))
        self.assertEqual(result['replies'][0]['text'], '[Customer Carl] 您好')

    def test_customer_chat_other_kind(self):
        # the customer chat labels a person who is neither a colleague nor a portal user [其他 <name>]
        chat = self.chat(self.portal)
        self.post(chat, self.portal, 'hi')
        self.assertEqual(self.call(chat, openai_ok('[其他 Otto]\u3000[負責同事 Sam] 好的'))['replies'][0]['text'], '好的')
        self.assertEqual(self.call(chat, openai_ok('[其他] 好的'))['replies'][0]['text'], '好的')
        stream = sse_response([sse_event(delta('[其')), sse_event(delta('他 Otto] 收到')), sse_event(delta(None, 'stop')),
                               'data: [DONE]\n\n'])
        self.connection.openai_stream = True
        self.assertEqual(self.call(chat, stream)['replies'][0]['text'], '收到', 'streamed')
        n8n_chat = self.chat(self.portal, self.n8n)
        self.post(n8n_chat, self.portal, 'hi')
        result = self.call(n8n_chat, http_response(200, {'replies': ['[其他 Otto] a', '[客戶] b']}), self.n8n)
        self.assertEqual([reply['text'] for reply in result['replies']], ['a', 'b'], 'n8n')

    def test_n8n_replies(self):
        chat = self.chat(self.internal, self.internal_n8n)
        self.post(chat, self.internal, 'hi')
        result = self.call(chat, http_response(200, {'replies': ['[負責同事 Staff Sam] a', '[Staff Sam] b',
                                                                 {'text': '[客戶]', 'attachments': []}, 'c']}),
                           self.internal_n8n)
        self.assertEqual([reply['text'] for reply in result['replies']], ['a', 'b', '', 'c'])
        result = self.call(chat, http_response(200, {'output': '[Staff Sam]'}), self.internal_n8n)
        self.assertEqual(result['call_log'].status, 'invalid_response', 'a reply of labels only, no file')
        result = self.call(chat, http_response(200, {'reply': {'text': '[真人客服 小美]', 'attachments': [
            {'name': 'a.txt', 'data_base64': 'aGk='}]}}), self.internal_n8n)
        self.assertEqual(result['call_log'].status, 'ok', 'an empty text with a file is a reply')
        self.assertEqual(result['replies'][0]['text'], '')

    def test_streamed_answer_as_a_whole(self):
        chat = self.chat(self.internal)  # a streaming connection (ProjectAiCase)
        self.post(chat, self.internal, 'hi')
        chunks = [sse_event(delta('[Staff')), sse_event(delta(' Sam]')), sse_event(delta(' [負責同事')),
                  sse_event(delta(' 王]\n')), sse_event(delta('好的')), sse_event(delta(None, 'stop')), 'data: [DONE]\n\n']
        result = self.call(chat, sse_response(chunks))
        self.assertEqual(result['call_log'].status, 'ok')
        self.assertEqual(result['replies'][0]['text'], '好的')


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestFileFallback(ProjectAiCase):

    def setUp(self):
        super().setUp()
        self.connection.openai_stream = False  # whole answers (a streamed turn: test_streamed_turn)

    def ask(self, chat=None, earlier=(), current=(), question='這是什麼？'):
        """In `chat` (default: the internal chat), Staff Sam posts the files `earlier` (name, raw), the AI
        answers, then he asks `question` with the files `current`. Returns {name: attachment}."""
        chat = chat or self.chat(self.internal)
        files = {}
        if earlier:
            atts = self.env['ir.attachment']
            for name, raw in earlier:
                files[name] = self.attachment(chat, self.internal, name=name, raw=raw)
                atts |= files[name]
            self.post(chat, self.internal, '先看這些', attachments=atts)
            self.assistant._post_text(chat, '收到。')
        atts = self.env['ir.attachment']
        for name, raw in current:
            files[name] = self.attachment(chat, self.internal, name=name, raw=raw)
            atts |= files[name]
        self.post(chat, self.internal, question, attachments=atts)
        return chat, files

    def run_latest(self, chat, effects):
        job = self.jobs(chat)[-1]
        with patch(BACKEND_POST, side_effect=list(effects)) as post, patch(BACKEND_SLEEP) as sleep:
            job._run_job(commit=False)
        return job, [json.loads(call.kwargs['data']) for call in post.call_args_list], sleep

    def logs(self, job):
        return self.env['woow.project.ai.call.log'].search([('job_id', '=', job.id)], order='id')

    def assert_link_only(self, payload, attachment, text_in):
        """`attachment` is no file part of `payload`, and the user turn holding `text_in` has its link line and
        the fallback note."""
        turn = next(turn for turn in payload['messages'][1:] if turn['role'] == 'user' and text_in in turn_text(turn))
        text = turn_text(turn)
        self.assertIn('[附件] %s（' % attachment.name, text)
        self.assertIn('/woow_project_ai/attachment/%s/' % attachment.id, text)
        self.assertIn('[附件說明] %s：%s' % (attachment.name, NOTE), text)
        self.assertLess(text.index('[附件] %s（' % attachment.name), text.index('[附件說明] %s：' % attachment.name),
                        'the note after the file\'s line')

    def test_each_trigger_status(self):
        for status in ai_assistant_backend.FALLBACK_HTTP_STATUSES:
            with self.subTest(status=status):
                chat = self.other_chat(self.internal, self.assistant)
                chat, files = self.ask(chat, earlier=[('menu.png', TINY_PNG)])
                job, sent, sleep = self.run_latest(chat, [refused(status), openai_ok('是一張菜單。')])
                self.assertEqual(len(sent), 2)
                self.assertEqual(len(file_parts(sent[0])), 1, 'the first request carries the image')
                self.assertFalse(file_parts(sent[1]), 'then without it')
                self.assert_link_only(sent[1], files['menu.png'], '先看這些')
                self.assertEqual(same_system(sent[1]), same_system(sent[0]), 'built again: the same instructions')
                self.assertEqual(sent[1]['messages'][-1], sent[0]['messages'][-1], 'the same question')
                sleep.assert_not_called()
                self.assertEqual(job.state, 'done')
                self.assertIn('是一張菜單。', str(self.bot_messages(chat)[-1].body))
                logs = self.logs(job)
                self.assertEqual([(log.status, log.http_status) for log in logs],
                                 [('http_error', status), ('ok', 200)], 'one call log row per request')
                self.assertEqual(logs[0].error, 'HTTP %s' % status)
                self.assertEqual(job.unreadable_files, str(files['menu.png'].id))

    def test_other_statuses_do_not_fall_back(self):
        cases = [([refused(401)], 1), ([refused(404)], 1), ([refused(502)], 1), ([refused(504)], 1),
                 ([refused(429), refused(429)], 2), ([refused(503), refused(503)], 2),
                 ([requests.ReadTimeout('slow')], 1)]
        for effects, calls in cases:
            with self.subTest(effects=effects):
                chat = self.other_chat(self.internal, self.assistant)
                chat, _files = self.ask(chat, earlier=[('menu.png', TINY_PNG)])
                job, sent, _sleep = self.run_latest(chat, effects + [openai_ok()])
                self.assertEqual(len(sent), calls, 'no request without the file')
                self.assertTrue(all(file_parts(payload) for payload in sent))
                self.assertEqual(job.state, 'failed')
                self.assertFalse(job.unreadable_files)
                self.assertEqual(len(self.logs(job)), 1)

    def test_documents_fall_back_too(self):
        chat, files = self.ask(earlier=[('報價.pdf', TINY_PDF), ('notes.txt', b'plain text notes')])
        job, sent, _sleep = self.run_latest(chat, [refused(422), openai_ok('看過了')])
        self.assertEqual([part['type'] for part in file_parts(sent[0])], ['file', 'file'])
        self.assertFalse(file_parts(sent[1]))
        for name in ('報價.pdf', 'notes.txt'):
            self.assert_link_only(sent[1], files[name], '先看這些')
        self.assertNotIn('檔案已附在這則訊息', turn_text(sent[1]['messages'][1]))
        self.assertEqual(job.state, 'done')
        self.assertEqual(set(job.unreadable_files.split(',')), {str(files['報價.pdf'].id), str(files['notes.txt'].id)})

    def test_the_question_s_own_file_is_left_out_last(self):
        chat, files = self.ask(earlier=[('menu.png', TINY_PNG)], current=[('合約.pdf', TINY_PDF)])
        job, sent, sleep = self.run_latest(chat, [refused(400), refused(500), openai_ok('好')])
        self.assertEqual(len(sent), 3)
        self.assertEqual(sorted(part['type'] for part in file_parts(sent[0])), ['file', 'image_url'])
        self.assertEqual([part['type'] for part in file_parts(sent[1])], ['file'], 'step 1: the question\'s file only')
        self.assert_link_only(sent[1], files['menu.png'], '先看這些')
        self.assertFalse(file_parts(sent[2]), 'step 2: no file part at all')
        self.assert_link_only(sent[2], files['menu.png'], '先看這些')
        self.assert_link_only(sent[2], files['合約.pdf'], '這是什麼？')
        sleep.assert_not_called()
        self.assertEqual([(log.status, log.http_status) for log in self.logs(job)],
                         [('http_error', 400), ('http_error', 500), ('ok', 200)])
        self.assertEqual(job.state, 'done')
        self.assertEqual(set(job.unreadable_files.split(',')), {str(files['menu.png'].id), str(files['合約.pdf'].id)})

    def test_only_the_question_has_a_file(self):
        chat, files = self.ask(current=[('scan.png', TINY_PNG)])
        job, sent, _sleep = self.run_latest(chat, [refused(415), openai_ok('好')])
        self.assertEqual(len(sent), 2, 'nothing earlier to leave out: at once without any file part')
        self.assertFalse(file_parts(sent[1]))
        self.assert_link_only(sent[1], files['scan.png'], '這是什麼？')
        self.assertEqual(job.unreadable_files, str(files['scan.png'].id))

    def test_no_success_fails_as_before_and_remembers_nothing(self):
        chat, _files = self.ask(earlier=[('menu.png', TINY_PNG)], current=[('合約.pdf', TINY_PDF)])
        job, sent, _sleep = self.run_latest(chat, [refused(500), refused(500), refused(500)])
        self.assertEqual(len(sent), 3)
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.error, 'HTTP 500')
        self.assertFalse(job.unreadable_files, 'nothing proved that a file was the problem')
        self.assertEqual(len(self.logs(job)), 3)
        error = self.bot_messages(chat)[-1]
        self.assertEqual(job.reply_message_id, error, 'the person gets the error message')
        self.assertIn('Sorry, I cannot answer right now.', str(error.body))

    def test_a_failing_fallback_request_ends_the_turn(self):
        chat, _files = self.ask(earlier=[('menu.png', TINY_PNG)], current=[('合約.pdf', TINY_PDF)])
        job, sent, _sleep = self.run_latest(chat, [refused(400), refused(401), openai_ok()])
        self.assertEqual(len(sent), 2, '401 is not a reason to leave out more files')
        self.assertEqual(job.state, 'failed')
        self.assertEqual([(log.status, log.http_status) for log in self.logs(job)],
                         [('http_error', 400), ('http_error', 401)])

    def test_each_fallback_request_is_built_again(self):
        # its own current time; the same choice of files minus the ones it leaves out
        chat, files = self.ask(earlier=[('menu.png', TINY_PNG)], current=[('合約.pdf', TINY_PDF)])
        times = [FROZEN, FROZEN + timedelta(minutes=1), FROZEN + timedelta(minutes=2)]
        with patch(BACKEND + '._utcnow', side_effect=times):
            job, sent, _sleep = self.run_latest(chat, [refused(400), refused(400), openai_ok('好')])
        lines = [[line for line in payload['messages'][0]['content'].split('\n') if line.startswith('Current time')]
                 for payload in sent]
        self.assertEqual([len(found) for found in lines], [1, 1, 1])
        self.assertEqual(len({found[0] for found in lines}), 3, 'a new current time for each request')
        self.assertEqual([part['type'] for part in file_parts(sent[1])], ['file'], 'the question\'s file kept')
        self.assertEqual(job.state, 'done')

    def test_the_gate_is_checked_again_before_a_fallback_request(self):
        # the AI connection stops being ready while the first request is answered: no fallback request is sent,
        # the job ends as a blocked one (the tab's notice), and the refused request keeps its only row
        chat, _files = self.ask(earlier=[('menu.png', TINY_PNG)])
        job = self.jobs(chat)[-1]

        def refuse_and_block(*args, **kwargs):
            self.connection.model = False  # readiness "blocked": the tab cannot answer
            return refused(400)

        with patch(BACKEND_POST, side_effect=refuse_and_block) as post:
            job._run_job(commit=False)
        self.assertEqual(post.call_count, 1, 'nothing more is sent')
        self.assertEqual(job.state, 'skipped')
        self.assertEqual([(log.status, log.http_status) for log in self.logs(job)], [('http_error', 400)])
        notices = self.env['mail.message'].sudo().search([('model', '=', 'discuss.channel'), ('res_id', '=', chat.id),
                                                          ('message_type', '=', 'notification')])
        self.assertTrue(notices, 'the blocked notice')
        self.assertFalse(job.unreadable_files)

    def test_the_gate_pause_and_access(self):
        # the customer chat is paused (a colleague wrote) while the first request is answered
        self.connection.openai_stream = False
        chat = self.chat(self.portal)
        att = self.attachment(chat, self.portal, name='photo.png', raw=TINY_PNG)
        self.post(chat, self.portal, '這是現場照片', attachments=att)
        job = self.jobs(chat)[-1]

        def refuse_and_pause(*args, **kwargs):
            chat.sudo()._woow_project_ai_pause(10)
            return refused(400)

        with patch(BACKEND_POST, side_effect=refuse_and_pause) as post:
            job._run_job(commit=False)
        self.assertEqual((post.call_count, job.state), (1, 'skipped'))
        # the asker loses the task access while the first request is answered
        internal, _files = self.ask(earlier=[('menu.png', TINY_PNG)])
        job = self.jobs(internal)[-1]

        def refuse_and_revoke(*args, **kwargs):
            # Staff Sam loses his Project rights: no longer of the internal chat's people
            self.internal.groups_id = [Command.unlink(self.env.ref('project.group_project_user').id)]
            self.env.invalidate_all()
            return refused(400)

        with patch(BACKEND_POST, side_effect=refuse_and_revoke) as post:
            job._run_job(commit=False)
        self.assertEqual((post.call_count, job.state), (1, 'skipped'))

    def test_a_failed_build_writes_no_second_row(self):
        # building the fallback request fails (a file deleted meanwhile): the refused request keeps its only row
        chat, _files = self.ask(earlier=[('menu.png', TINY_PNG)])
        job = self.jobs(chat)[-1]
        Assistant = type(self.env['woow.project.ai.assistant'])
        build = Assistant._build_request
        calls = []

        def build_request(assistant, *args, **kwargs):
            calls.append(kwargs.get('files') is not None)
            if kwargs.get('files') is not None:
                raise MissingError('Record does not exist or has been deleted.')
            return build(assistant, *args, **kwargs)

        with patch.object(Assistant, '_build_request', autospec=True, side_effect=build_request), \
                patch(BACKEND_POST, side_effect=[refused(400), openai_ok()]) as post, \
                self.assertLogs('odoo.addons.woow_ai_task.models.ai_assistant_backend', level='ERROR') as logs:
            job._run_job(commit=False)
        self.assertIn('MissingError', '\n'.join(logs.output), 'the type name, not the text')
        self.assertEqual(calls, [False, True])
        self.assertEqual(post.call_count, 1)
        self.assertEqual([(log.status, log.http_status, log.error) for log in self.logs(job)],
                         [('http_error', 400, 'HTTP 400')], 'no second (error) row for the refused request')
        self.assertEqual((job.state, job.error), ('failed', 'HTTP 400'))

    def test_commit_post_commit_post(self):
        # no transaction is open during a request: the job commits before each one, the fallback request too
        chat, _files = self.ask(earlier=[('menu.png', TINY_PNG)])
        history = chat._woow_project_ai_history(self.assistant, chat._woow_project_ai_context_messages(self.assistant))
        order = []
        answers = [refused(400), openai_ok('好')]

        def post(*args, **kwargs):
            order.append('post')
            return answers.pop(0)

        with patch(BACKEND_POST, side_effect=post):
            result = self.assistant._call_backend(chat, history, job=self.jobs(chat)[-1],
                                                  before_call=lambda: order.append('commit'))
        self.assertEqual(order, ['commit', 'post', 'commit', 'post'])
        self.assertFalse(result['error'])

    def test_less_than_a_second_left(self):
        for left, calls in ((0.5, 1), (1.5, 2)):
            with self.subTest(left=left):
                chat, _files = self.ask(self.other_chat(self.internal, self.assistant), earlier=[('menu.png', TINY_PNG)])
                job = self.jobs(chat)[-1]
                clock = Clock()
                answers = [refused(400), openai_ok('好')]

                def answer(*args, **kwargs):
                    if len(answers) == 2:
                        clock.now += 300 - left
                    return answers.pop(0)

                with patch(BACKEND + '.time', clock), patch(BACKEND_POST, side_effect=answer) as post:
                    job._run_job(commit=False)
                self.assertEqual(post.call_count, calls)
                if calls == 2:
                    self.assertEqual(post.call_args_list[1].kwargs['timeout'], (10, 1.5))
                    self.assertEqual(job.state, 'done')
                else:
                    self.assertEqual(job.state, 'failed')

    def test_remembered_files_where_they_would_be_file_parts(self):
        # a remembered file is its link line and the note only where it would be a file part
        chat, files = self.ask(current=[('menu.png', TINY_PNG), ('報價.pdf', TINY_PDF)])
        self.run_latest(chat, [refused(400), openai_ok('好')])
        self.assertEqual(self.Job._unreadable_files_of(chat), {files['menu.png'].id, files['報價.pdf'].id})
        # in the window: link and note
        self.post(chat, self.internal, '再一次')
        _job, sent, _sleep = self.run_latest(chat, [openai_ok('好')])
        self.assertFalse(file_parts(sent[0]))
        self.assert_link_only(sent[0], files['menu.png'], '這是什麼？')
        self.assert_link_only(sent[0], files['報價.pdf'], '這是什麼？')
        # documents sent as links (document_mode 'link'): the PDF as usual (no note); the image is still a file
        # part in that mode, so it keeps its note
        self.connection.document_mode = 'link'
        self.post(chat, self.internal, '連結模式')
        _job, sent, _sleep = self.run_latest(chat, [openai_ok('好')])
        text = turn_text(next(turn for turn in sent[0]['messages'] if '這是什麼？' in turn_text(turn)))
        self.assertIn('[附件] 報價.pdf（', text)
        self.assertNotIn('[附件說明] 報價.pdf', text)
        self.assertIn('[附件說明] menu.png：%s' % NOTE, text)
        self.connection.document_mode = 'file'
        # outside the window (no longer among the last 3 user messages for images, 5 for documents): as usual
        for i in range(ai_assistant_backend.DOC_MESSAGES):
            self.post(chat, self.internal, 'filler %s' % i)
        _job, sent, _sleep = self.run_latest(chat, [openai_ok('好')])
        text = turn_text(next(turn for turn in sent[0]['messages'] if '這是什麼？' in turn_text(turn)))
        self.assertIn('[附件] menu.png（', text)
        self.assertNotIn(NOTE, text, 'no fallback note outside the window')
        self.assertIn('[附件說明] 報價.pdf：在較早的訊息中', text, 'the usual note of an older document')

    def test_a_remembered_file_too_large_gets_its_usual_note(self):
        chat, files = self.ask(earlier=[('報價.pdf', TINY_PDF)])
        self.run_latest(chat, [refused(400), openai_ok('好')])
        self.post(chat, self.internal, '再一次')
        with patch(BACKEND + '.DOC_FILE_MAX_BYTES', 10):
            _job, sent, _sleep = self.run_latest(chat, [openai_ok('好')])
        text = turn_text(next(turn for turn in sent[0]['messages'] if '先看這些' in turn_text(turn)))
        self.assertIn('[附件說明] 報價.pdf：檔案太大', text)
        self.assertNotIn(NOTE, text)

    def test_the_memory_lasts_as_long_as_the_conversation(self):
        chat, files = self.ask(current=[('menu.png', TINY_PNG)])
        first, _sent, _sleep = self.run_latest(chat, [refused(400), openai_ok('好')])
        self.post(chat, self.internal, '第二題')
        second, _sent, _sleep = self.run_latest(chat, [openai_ok('好')])
        self.assertEqual(second.unreadable_files, str(files['menu.png'].id), 'each job that asks carries the set')
        # every job is older than the cleanup's 30 days: the newest one carrying the memory stays
        self.env.cr.execute("UPDATE woow_project_ai_job SET create_date = now() - interval '40 days' WHERE id IN %s",
                            [tuple(self.jobs(chat).ids)])
        self.env.invalidate_all()
        self.Job._cleanup_old_jobs()
        self.assertEqual(self.jobs(chat), second, 'the newest job carrying the memory stays')
        self.assertFalse(first.exists())
        # a new model does not clear it
        self.connection.model = 'another-model'
        self.post(chat, self.internal, '第三題')
        third, sent, _sleep = self.run_latest(chat, [openai_ok('好')])
        self.assert_link_only(sent[0], files['menu.png'], '這是什麼？')
        # a system administrator does: the next request sends the file as a file part again
        third.action_forget_unreadable_files()
        self.assertFalse(self.jobs(chat).filtered('unreadable_files'))
        self.assertEqual(self.Job._unreadable_files_of(chat), frozenset())
        history = chat._woow_project_ai_history(self.assistant, chat._woow_project_ai_context_messages(self.assistant))
        _url, _headers, _body, payload = self.assistant._build_request(chat, history, third.message_id)
        self.assertEqual(len(file_parts(payload)), 1, 'sent as a file part again')

    def test_the_deadline_stops_the_fallback(self):
        chat, _files = self.ask(earlier=[('menu.png', TINY_PNG)])
        job = self.jobs(chat)[-1]
        clock = Clock()

        def slow_refusal(*args, **kwargs):
            clock.now += 301  # the connection's 300 s are over when the answer comes
            return refused(400)

        with patch(BACKEND + '.time', clock), patch(BACKEND_POST, side_effect=slow_refusal) as post:
            job._run_job(commit=False)
        self.assertEqual(post.call_count, 1, 'no request after the deadline')
        self.assertEqual(job.state, 'failed')
        self.assertEqual([(log.status, log.http_status) for log in self.logs(job)], [('http_error', 400)])

    def test_the_fallback_waits_only_for_what_is_left(self):
        chat, _files = self.ask(earlier=[('menu.png', TINY_PNG)])
        job = self.jobs(chat)[-1]
        clock = Clock()
        answers = [refused(400), openai_ok('好')]

        def answer(*args, **kwargs):
            clock.now += 200
            return answers.pop(0)

        with patch(BACKEND + '.time', clock), patch(BACKEND_POST, side_effect=answer) as post:
            job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        self.assertEqual(post.call_args_list[0].kwargs['timeout'], (10, 300))
        self.assertEqual(post.call_args_list[1].kwargs['timeout'], (10, 100), 'the same deadline bounds it')

    def test_not_a_retry(self):
        self.connection.max_retries = 0
        chat, _files = self.ask(earlier=[('menu.png', TINY_PNG)])
        job, sent, sleep = self.run_latest(chat, [refused(400), openai_ok('好')])
        self.assertEqual((len(sent), job.state), (2, 'done'), 'max_retries does not count it')
        sleep.assert_not_called()
        # the usual retries still happen first, and the fallback request gets its own
        self.connection.max_retries = 1
        chat, _files = self.ask(self.other_chat(self.internal, self.assistant), earlier=[('menu.png', TINY_PNG)])
        job, sent, sleep = self.run_latest(chat, [refused(429), refused(400), refused(503), openai_ok('好')])
        self.assertEqual((len(sent), job.state), (4, 'done'))
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3, 3])
        self.assertEqual([bool(file_parts(payload)) for payload in sent], [True, True, False, False])

    def test_the_conversation_remembers(self):
        chat, files = self.ask(earlier=[('menu.png', TINY_PNG)])
        job, sent, _sleep = self.run_latest(chat, [refused(400), openai_ok('是菜單')])
        self.assertEqual(len(sent), 2)
        self.assertEqual(self.Job._unreadable_files_of(chat), frozenset([files['menu.png'].id]))
        # the next turn: one request, the image as its line and the note at once; a new image goes inline
        new = self.attachment(chat, self.internal, name='new.png', raw=TINY_PNG)
        self.post(chat, self.internal, '那這張呢？', attachments=new)
        job2, sent2, _sleep = self.run_latest(chat, [openai_ok('是新的圖')])
        self.assertEqual(len(sent2), 1, 'no first failing request')
        self.assert_link_only(sent2[0], files['menu.png'], '先看這些')
        self.assertEqual(len(file_parts(sent2[0])), 1, 'only the new image is a file part')
        self.assertEqual(job2.state, 'done')
        self.assertEqual(job2.unreadable_files, str(files['menu.png'].id), 'each job that asks carries the set')
        # the job that left the file out is gone: the newer job still remembers
        job.unlink()
        self.assertEqual(self.Job._unreadable_files_of(chat), frozenset([files['menu.png'].id]))
        # the next question: the image is no longer among the last 3 user messages (no file part anyway): its
        # usual link line, without the note; the job still carries the memory
        self.post(chat, self.internal, '還記得嗎？')
        job3, sent3, _sleep = self.run_latest(chat, [openai_ok('記得')])
        text = turn_text(next(turn for turn in sent3[0]['messages'] if '先看這些' in turn_text(turn)))
        self.assertIn('[附件] menu.png（', text)
        self.assertNotIn(NOTE, text)
        self.assertEqual(job3.unreadable_files, str(files['menu.png'].id))
        self.assertEqual(len(self.Job._unreadable_files_of(self.chat(self.portal))), 0, 'per conversation')

    def test_a_failed_turn_adds_nothing_and_keeps_what_was_known(self):
        chat, files = self.ask(earlier=[('menu.png', TINY_PNG)])
        self.run_latest(chat, [refused(400), openai_ok('是菜單')])
        new = self.attachment(chat, self.internal, name='new.png', raw=TINY_PNG)
        self.post(chat, self.internal, '那這張呢？', attachments=new)
        job, sent, _sleep = self.run_latest(chat, [refused(500), refused(500)])
        self.assertEqual(len(sent), 2, 'the new image, then without it')
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.unreadable_files, str(files['menu.png'].id), 'the new image is not remembered')
        self.assertEqual(self.Job._unreadable_files_of(chat), frozenset([files['menu.png'].id]))

    def test_the_memory_is_durable_before_the_downloads(self):
        chat, _files = self.ask(earlier=[('menu.png', TINY_PNG)])
        job = self.jobs(chat)[-1]
        events = []
        Assistant = type(self.env['woow.project.ai.assistant'])
        prepare = Assistant._prepare_reply

        def commit_step():
            self.env.flush_all()
            events.append(('commit', bool(job.unreadable_files)))

        def prepare_reply(assistant, reply, **kwargs):
            events.append(('prepare', None))
            return prepare(assistant, reply, **kwargs)

        self.assertTrue(job._try_lock_channel(chat.id))
        try:
            with patch(BACKEND_POST, side_effect=[refused(400), openai_ok('好')]), \
                    patch.object(Assistant, '_prepare_reply', autospec=True, side_effect=prepare_reply):
                job._process_locked(commit_step, commit=False)
        finally:
            job._unlock_channel(chat.id)
        self.assertLess(events.index(('commit', True)), events.index(('prepare', None)))

    def test_a_retry_of_the_job_remembers(self):
        # the run crashes after the answer came (the memory was committed before: the test above); in tests
        # (commit=False) the error propagates and nothing is rolled back, as a real run keeps its last commit
        chat, files = self.ask(earlier=[('menu.png', TINY_PNG)])
        job = self.jobs(chat)[-1]
        Assistant = type(self.env['woow.project.ai.assistant'])
        crashed = False
        with patch(BACKEND_POST, side_effect=[refused(400), openai_ok('好')]), patch(BACKEND_SLEEP), \
                patch.object(Assistant, '_post_replies', side_effect=RuntimeError('crash while posting')):
            try:
                job._run_job(commit=False)
            except RuntimeError:
                crashed = True
        self.assertTrue(crashed)
        self.assertEqual((job.state, job.run_count), ('running', 1))
        self.assertEqual(job.unreadable_files, str(files['menu.png'].id))
        # lost owner: the runner takes the job again
        job.write({'started_at': fields.Datetime.now() - timedelta(minutes=2)})
        retried, sent, _sleep = self.run_latest(chat, [openai_ok('好')])
        self.assertEqual(retried, job)
        self.assertEqual(len(sent), 1, 'the retry sends the image as a link at once')
        self.assert_link_only(sent[0], files['menu.png'], '先看這些')
        self.assertEqual((job.state, job.run_count), ('done', 2))

    def test_no_file_no_fallback(self):
        chat, _files = self.ask()
        job, sent, _sleep = self.run_latest(chat, [refused(500), openai_ok()])
        self.assertEqual(len(sent), 1)
        self.assertEqual(job.state, 'failed')
        self.assertEqual(len(self.logs(job)), 1)

    def test_link_mode_sends_no_file_part(self):
        self.connection.document_mode = 'link'
        chat, _files = self.ask(earlier=[('報價.pdf', TINY_PDF)])
        job, sent, _sleep = self.run_latest(chat, [refused(400), openai_ok()])
        self.assertEqual(len(sent), 1, 'a document sent as a link is no file part: nothing to leave out')
        self.assertEqual(job.state, 'failed')

    def test_n8n_unchanged(self):
        chat = self.chat(self.internal, self.internal_n8n)
        att = self.attachment(chat, self.internal, name='報價.pdf', raw=TINY_PDF)
        self.post(chat, self.internal, '看附件', attachments=att)
        job, sent, _sleep = self.run_latest(chat, [refused(400), http_response(200, {'reply': 'ok'})])
        self.assertEqual(len(sent), 1, 'n8n gets links: no fallback')
        self.assertEqual(job.state, 'failed')
        self.assertFalse(job.unreadable_files)

    def test_streamed_turn(self):
        self.connection.openai_stream = True
        chat, files = self.ask(earlier=[('menu.png', TINY_PNG)])
        stream = sse_response([sse_event(delta('[負責同事 x] 看到了')), sse_event(delta(None, 'stop')), 'data: [DONE]\n\n'])
        job, sent, _sleep = self.run_latest(chat, [refused(400), stream])
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[1]['stream'])
        self.assert_link_only(sent[1], files['menu.png'], '先看這些')
        self.assertEqual(job.state, 'done')
        body = str(self.bot_messages(chat)[-1].body)
        self.assertIn('看到了', body)
        self.assertNotIn('負責同事', body)

    def test_call_log_payloads_stay_redacted(self):
        self.connection.log_payload = True
        chat, _files = self.ask(earlier=[('menu.png', TINY_PNG)])
        job, _sent, _sleep = self.run_latest(chat, [refused(400), openai_ok('好')])
        first, second = self.logs(job)
        self.assertIn('data:image/png;base64,<', first.request_payload)
        self.assertIn(NOTE, second.request_payload)
        self.assertNotIn('base64,iVBOR', first.request_payload + second.request_payload)


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestExplicitJobRow(ProjectAiCase):

    def test_ir_default_fills_no_job_field(self):
        Default = self.env['ir.default'].sudo()
        Default.set('woow.project.ai.job', 'unreadable_files', '1,2,3')
        Default.set('woow.project.ai.job', 'context_message_id', 99)
        Default.set('woow.project.ai.job', 'finished_at', '2099-01-01 00:00:00')
        Default.set('woow.project.ai.job', 'unreadable_files', '4,5', user_id=self.internal.id)
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'hi')
        job = self.jobs(chat)[-1]
        self.assertFalse(job.unreadable_files)
        self.assertEqual(job.context_message_id, 0)
        self.assertFalse(job.finished_at)
        self.assertEqual(self.Job._unreadable_files_of(chat), frozenset())

    def test_forget_files_sent_as_links(self):
        """The Reply Job form's button (system administrators): the job's whole conversation forgets what it
        remembers (every job of that chat), other conversations keep theirs."""
        chat = self.chat(self.internal)
        other = self.other_chat(self.internal, self.assistant)
        for channel, files in ((chat, [11, 12]), (chat, [11]), (other, [21])):
            self.post(channel, self.internal, 'hi')
            self.jobs(channel)[-1]._remember_unreadable_files(files)
        job = self.jobs(chat)[0]
        user = self.internal  # an employee with Project rights, not a system administrator
        with self.assertRaises(AccessError):
            job.with_user(user).action_forget_unreadable_files()
        self.assertEqual(self.Job._unreadable_files_of(chat), frozenset([11, 12]))
        self.assertIs(job.action_forget_unreadable_files(), True)
        self.assertFalse(self.jobs(chat).filtered('unreadable_files'), 'every job of the conversation')
        self.assertEqual(self.Job._unreadable_files_of(chat), frozenset())
        self.assertEqual(self.Job._unreadable_files_of(other), frozenset([21]), 'another conversation keeps its own')
        view = self.env.ref('woow_ai_task.view_woow_project_ai_job_form')
        self.assertIn('action_forget_unreadable_files', view.arch_db)

    def test_memory_values(self):
        self.assertEqual(self.Job._file_ids('12, 15,x,,7'), {12, 15, 7})
        self.assertEqual(self.Job._file_ids('²,①,٣,3'), {3}, 'ASCII digits only: no crash')
        self.assertEqual(self.Job._file_ids(False), set())
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'hi')
        job = self.jobs(chat)[-1]
        self.assertTrue(job._remember_unreadable_files([5, 3]))
        self.assertTrue(job._remember_unreadable_files([3, 9]))
        self.assertFalse(job._remember_unreadable_files([9]), 'nothing new: no write')
        self.assertEqual(job.unreadable_files, '3,5,9')
        self.assertFalse(job.copy_data()[0].get('unreadable_files'), 'never copied')


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestPushLabels(AiTaskFixtures, HttpCase):
    """A push is a later reply posted as the AI: the labels of answers go from it too."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()

    def setUp(self):
        super().setUp()
        patcher = patch.object(type(self.env['woow.project.ai.job']), '_start_after_commit')
        patcher.start()
        self.addCleanup(patcher.stop)

    def push(self, assistant, channel, text):
        key = assistant.action_generate_push_key()['context']['default_push_key']
        response, _counts = run_after_commit_usage(self, lambda: self.url_open(
            '/woow_project_ai/v1/push', data=json.dumps({'to': {'channel_id': channel.id}, 'text': text}),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer %s' % key}))
        return response.status_code, response.json()

    def test_push_loses_the_labels(self):
        customer = self.open_tab(self.customer, 'customer')
        status, data = self.push(self.customer_ai, customer, '<think>x</think>[負責同事 Staff Sam] 報價已更新。')
        self.assertEqual(status, 200, data)
        body = str(self.env['mail.message'].sudo().browse(data['message_id']).body)
        self.assertIn('報價已更新。', body)
        self.assertNotIn('負責同事', body)
        status, data = self.push(self.customer_ai, customer, '[其他 Otto]\u00a0[客戶] 稍後回覆。')
        self.assertEqual(status, 200, data)
        self.assertNotIn('其他', str(self.env['mail.message'].sudo().browse(data['message_id']).body))
        status, data = self.push(self.customer_ai, customer, '[客戶 Customer Carl]')
        self.assertEqual((status, data['error']), (400, 'empty_message'), 'only a label: refused like an empty text')

    def test_push_uses_the_names_of_answers(self):
        # the internal chat: a member's label, and the label of a history author who left the chat
        tina_chat = self.open_tab(self.staff2, 'internal')
        self.post(tina_chat, self.staff2, '我明天去現場')
        tina_chat.with_user(self.staff2).sudo().action_unfollow()
        internal = self.open_tab(self.staff, 'internal')
        self.assertEqual(internal, tina_chat)
        for text, kept, gone in (('[Staff Sam] 明天到。', '明天到。', '[Staff Sam]'),
                                 ('[Staff Tina] 我去。', '我去。', '[Staff Tina]'),
                                 ('[Staff Sam] 報價\n[Staff Tina] 安裝', '[Staff Sam] 報價', None)):
            status, data = self.push(self.internal_ai, internal, text)
            self.assertEqual(status, 200, data)
            body = str(self.env['mail.message'].sudo().browse(data['message_id']).body)
            self.assertIn(kept, body, text)
            if gone:
                self.assertNotIn(gone, body, text)
