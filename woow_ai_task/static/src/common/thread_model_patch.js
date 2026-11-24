/** @odoo-module */
// Task AI conversations (woow_ai_task, the channel data carries
// "woow_project_ai_tab"): no calls (the AI never answers one), no invitations
// (members follow the task access; the server refuses them) and no "You
// unsubscribed from ..." toast when the task access is lost (the member was
// removed, they did not leave). Loaded in the backend and on Odoo's public
// Discuss page (portal task page). The templates (task_chat_templates.xml)
// hide the member panel's invite button and the avatar upload.
import { threadActionsRegistry } from "@mail/core/common/thread_actions";
import { Thread } from "@mail/core/common/thread_model";
import "@mail/discuss/core/common/thread_actions"; // registers "invite-people"
import "@mail/discuss/core/common/thread_model_patch"; // defines notifyOnLeave
import { patch } from "@web/core/utils/patch";

patch(Thread.prototype, {
    get allowCalls() {
        return super.allowCalls && !this.woow_project_ai_tab;
    },
    get notifyOnLeave() {
        return super.notifyOnLeave && !this.woow_project_ai_tab;
    },
});

const inviteAction = threadActionsRegistry.get("invite-people", null);
if (inviteAction) {
    patch(inviteAction, {
        condition(component) {
            return !component.thread?.woow_project_ai_tab && super.condition(...arguments);
        },
    });
}
