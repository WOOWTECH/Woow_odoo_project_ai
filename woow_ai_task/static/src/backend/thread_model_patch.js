/** @odoo-module */
// A task AI conversation shown in the task form (task_chat.js) counts as
// displayed, as in Discuss or an open chat window: its new messages raise no
// web push and no chat window / bubble in the chat hub while it is shown.
import { Record } from "@mail/core/common/record";
import { Thread } from "@mail/core/common/thread_model";
import "@mail/core/public_web/thread_model_patch"; // defines inChathubOnNewMessage
import { patch } from "@web/core/utils/patch";

patch(Thread.prototype, {
    setup() {
        super.setup(...arguments);
        this.woowProjectAiEmbedded = Record.attr(false);
    },
    computeIsDisplayed() {
        return Boolean(this.woowProjectAiEmbedded) || super.computeIsDisplayed();
    },
    get inChathubOnNewMessage() {
        return super.inChathubOnNewMessage && !this.woowProjectAiEmbedded;
    },
});
