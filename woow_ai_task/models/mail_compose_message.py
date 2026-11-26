# -*- coding: utf-8 -*-
"""The full composer's fields computed from its parent message (model,
res_ids, record name, subject, recipients) are stored computes, which Odoo
runs as superuser, also in an onchange: a parent in a task AI conversation is
refused unless the real user may read that conversation (the message's own
read check), so that no composer maps a task-chat message to its chat or
lists who was mentioned in it."""

from odoo import SUPERUSER_ID, models

from ..tools.ids import as_id


class MailComposeMessage(models.TransientModel):
    _inherit = 'mail.compose.message'

    # (Odoo collects the dependencies of a compute from every override: Odoo's own stay)

    def _woow_project_ai_check_parents(self):
        """Refuse when a parent of these composers (new records included) is a
        task-chat message the real user (env.uid; not the superuser account)
        may not read."""
        if self.env.uid in (None, SUPERUSER_ID):
            return
        ids = set()
        for composer in self:
            parent = composer.parent_id
            parent_id = as_id(parent._origin.id or parent.id) if parent else None
            if parent_id:
                ids.add(parent_id)
        if ids:
            self.env(su=False)['mail.message'].browse(sorted(ids))._woow_project_ai_check_records()

    def _compute_subject(self):
        self._woow_project_ai_check_parents()
        return super()._compute_subject()

    def _compute_model(self):
        self._woow_project_ai_check_parents()
        return super()._compute_model()

    def _compute_res_ids(self):
        self._woow_project_ai_check_parents()
        return super()._compute_res_ids()

    def _compute_record_name(self):
        self._woow_project_ai_check_parents()
        return super()._compute_record_name()

    def _compute_partner_ids(self):
        self._woow_project_ai_check_parents()
        return super()._compute_partner_ids()
