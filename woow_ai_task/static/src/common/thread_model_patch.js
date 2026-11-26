/** @odoo-module */
// Task AI conversations (woow_ai_task, the channel data carries
// "woow_project_ai_tab"): no calls (the AI never answers one), no invitations
// (members follow the task access; the server refuses them) and no "You
// unsubscribed from ..." toast when the task access is lost (the member was
// removed, they did not leave). Loaded in the backend and on Odoo's public
// Discuss page (portal task page). The templates (task_chat_templates.xml)
// hide the member panel's invite button and the avatar upload.
// A colleague writing in a customer chat (task form, Discuss, a chat window,
// the portal page) reads that the customers get the message and the AI
// pauses. On the portal task page the chat cannot be closed (closing it
// would leave Odoo's conversation list in the task's section).
import { ChatWindow } from "@mail/core/common/chat_window";
import { Composer } from "@mail/core/common/composer";
import { threadActionsRegistry } from "@mail/core/common/thread_actions";
import { Thread } from "@mail/core/common/thread_model";
import "@mail/discuss/core/common/thread_actions"; // registers "invite-people"
import "@mail/discuss/core/common/thread_model_patch"; // defines notifyOnLeave
import { _t } from "@web/core/l10n/translation";
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

patch(Composer.prototype, {
    get placeholder() {
        if (
            !this.props.placeholder &&
            this.thread?.woow_project_ai_tab === "customer" &&
            this.store?.self?.isInternalUser
        ) {
            return _t("Message to the customer: the AI pauses after you write…");
        }
        return super.placeholder;
    },
});

function isPublicTaskChat(component) {
    return Boolean(component.store?.inPublicPage && component.thread?.woow_project_ai_tab);
}

const closeAction = threadActionsRegistry.get("close", null);
if (closeAction) {
    patch(closeAction, {
        condition(component) {
            return !isPublicTaskChat(component) && super.condition(...arguments);
        },
    });
}

patch(ChatWindow.prototype, {
    async close(options) {
        if (isPublicTaskChat(this)) {
            return; // Escape included: the portal task page keeps its chat
        }
        return super.close(...arguments);
    },
});
