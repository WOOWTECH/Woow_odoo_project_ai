# -*- coding: utf-8 -*-
"""The old task AI's routes are refused while the switch is off, before their
endpoint runs (nothing is read, posted, uploaded or spawned: the stream
route is the only path that spawns an agent pod): every route under
/api/ai/ (8 today, only the old task AI's), and any other URL bound to one
of its 8 methods. /woow reads one flag of the session info to hide the old
Chat tab and status pills."""

from werkzeug.exceptions import Gone

from odoo import models
from odoo.exceptions import UserError
from odoo.http import request

from .legacy import legacy_rule, legacy_task_ai_enabled, refusal


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    @classmethod
    def _pre_dispatch(cls, rule, args):
        super()._pre_dispatch(rule, args)
        # every request passes here: a prefix test and two attribute reads first, the switch only for the old
        # routes (by URL rule, or by endpoint: another URL on one of the 8 methods)
        if legacy_rule(rule) and not legacy_task_ai_enabled(request.env):
            text = refusal(request.env)
            if rule.endpoint.routing.get('type') == 'http':
                raise Gone(text)  # HTTP 410 (the upload and the stream)
            raise UserError(text)  # JSON-RPC: an error carrying the text

    def session_info(self):
        info = super().session_info()
        info['woow_project_ai_paas_legacy'] = legacy_task_ai_enabled(self.env)
        return info
