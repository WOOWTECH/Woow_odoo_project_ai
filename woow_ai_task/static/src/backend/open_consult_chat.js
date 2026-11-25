/** @odoo-module */
// 「詢問 AI 顧問」 / 「開新議題」 (woow.project.ai.consult.mixin): the server opened (created or joined) the
// internal AI chat of the consultation task; open it in a chat window, so that the user stays on the record.
// Odoo's own pattern for a channel link (mail store_service handleClickOnLink): Thread.getOrFetch (the Discuss
// patch fetches the channel's info), then thread.open(). If that fails, the task form opens instead.
import { registry } from "@web/core/registry";

async function openConsultChat(env, action) {
    const { channel_id: channelId, task_id: taskId } = action.params || {};
    const store = env.services["mail.store"];
    if (store && channelId) {
        try {
            const thread = await store.Thread.getOrFetch({ model: "discuss.channel", id: channelId });
            if (thread) {
                thread.open();
                return;
            }
        } catch {
            // the task form below
        }
    }
    if (!taskId) {
        return;
    }
    return {
        type: "ir.actions.act_window",
        res_model: "project.task",
        res_id: taskId,
        views: [[false, "form"]],
        target: "current",
    };
}

registry.category("actions").add("woow_project_ai_open_consult_chat", openConsultChat);
