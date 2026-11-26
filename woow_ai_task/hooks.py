# -*- coding: utf-8 -*-
"""Install / uninstall hooks."""

import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """The new column discuss_channel.woow_project_ai_task_id gets its
    statistics at once (autovacuum would analyse a large discuss_channel only
    later): PostgreSQL plans the global channel rule's ``IS NULL`` leaf with
    them, in every channel query."""
    env.cr.execute("ANALYZE discuss_channel")


def uninstall_hook(env):
    """Without this module nobody keeps the members of the task AI
    conversations in line with the task access (no sync, no post check, no
    read rule), so their people are removed (the AI assistants' bots stay;
    in two statements, whatever the number of chats) and they are archived:
    history kept, readable by system administrators only.
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
    bots = sorted(Channel._woow_project_ai_bot_partner_ids())
    # the people leave every task chat at once: two statements, not member by member with its notices (a tenant
    # may have thousands of chats, tens of thousands of members: one web request). Their unread notifications of
    # the chats' messages are marked read first; no "left" notice and no bus notice per member: the chats are
    # archived next, and the clients drop them at their next load
    env.flush_all()
    env.cr.execute("""
        UPDATE mail_notification n SET is_read = TRUE, read_date = now() AT TIME ZONE 'UTC'
          FROM mail_message m JOIN discuss_channel c ON c.id = m.res_id AND c.woow_project_ai_task_id IS NOT NULL
         WHERE m.id = n.mail_message_id AND m.model = 'discuss.channel' AND n.is_read IS NOT TRUE
           AND NOT n.res_partner_id = ANY(%s)""", [bots])
    env.cr.execute("""
        DELETE FROM discuss_channel_member m USING discuss_channel c
         WHERE c.id = m.channel_id AND c.woow_project_ai_task_id IS NOT NULL
           AND (m.partner_id IS NULL OR NOT m.partner_id = ANY(%s))""", [bots])
    removed = env.cr.rowcount
    env.invalidate_all()
    active = channels.filtered('active')
    if active:
        active.write({'active': False})
    partners = env['woow.project.ai.assistant'].sudo().with_context(active_test=False).search([]).partner_id
    partners = partners.with_context(active_test=False).filtered('active')
    if partners:
        partners.write({'active': False})
    _logger.info("woow_ai_task uninstalled: %s task AI conversation(s) archived, %s member(s) removed",
                 len(channels), removed)
