# -*- coding: utf-8 -*-
"""Install / uninstall hooks."""

import logging

_logger = logging.getLogger(__name__)


def uninstall_hook(env):
    """Without this module nobody keeps the members of the task AI
    conversations in line with the task access (no sync, no post check, no
    read rule), so their people are removed (the AI assistants' bots stay) and
    they are archived: history kept, readable by system administrators only.
    The bot partners stay archived (they author the old AI messages). The
    system parameters the code writes (the cron runner report, the sweep's
    cursor: no xmlid) are removed."""
    # first, the global discuss.channel rule: it reads a field of this module, so it must never outlive its
    # code (Odoo's uninstall deletes the module's records, the rule included, before its fields anyway)
    rule = env.ref('woow_ai_task.rule_discuss_channel_woow_project_ai', raise_if_not_found=False)
    if rule:
        rule.sudo().active = False
    # system parameters written by the code (no xmlid): the cron runner report and the sweep's cursor
    env.cr.execute("DELETE FROM ir_config_parameter WHERE key IN %s",
                   (('woow_ai_task.dispatcher_state', 'woow_ai_task.sweep_cursor'),))
    Channel = env['discuss.channel'].sudo().with_context(active_test=False)
    channels = Channel.search([('woow_project_ai_task_id', '!=', False)])
    bots = Channel._woow_project_ai_bot_partner_ids()
    people = channels.channel_member_ids.filtered(lambda member: member.partner_id.id not in bots)
    if people:
        Channel._woow_project_ai_remove_members(people, reason='woow_ai_task uninstalled')
    active = channels.filtered('active')
    if active:
        active.write({'active': False})
    partners = env['woow.project.ai.assistant'].sudo().with_context(active_test=False).search([]).partner_id
    partners = partners.with_context(active_test=False).filtered('active')
    if partners:
        partners.write({'active': False})
    _logger.info("woow_ai_task uninstalled: %s task AI conversation(s) archived, %s member(s) removed",
                 len(channels), len(people))
