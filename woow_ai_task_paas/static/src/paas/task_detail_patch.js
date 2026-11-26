/** @odoo-module **/
// /woow task page: a "Customer AI Chat" tab (Project AI's customer chat of the task, Odoo's own Discuss page
// embedded) when the server says the current user may open it; the old "Chat" tab only while the old task AI
// is on. The page has no t-key and loads its task only when mounted, so a new taskId resets the tab here.
import { onWillUpdateProps, useState } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";
import { TaskDetailPage } from "@woow_paas_platform/paas/pages/task-detail/TaskDetailPage";
import { jsonRpc } from "@woow_paas_platform/paas/services/rpc";
import { legacyTaskAi } from "./legacy_flag";
import { WoowProjectAiPaasTaskChat } from "./task_chat/WoowProjectAiPaasTaskChat";

export const WOOW_PROJECT_AI_PAAS_TAB = "woow_project_ai_customer";

TaskDetailPage.components = { ...TaskDetailPage.components, WoowProjectAiPaasTaskChat };

patch(TaskDetailPage.prototype, {
  setup() {
    super.setup(...arguments);
    this.woowProjectAiPaas = useState({ info: null });
    this.woowProjectAiPaasLegacy = legacyTaskAi();
    onWillUpdateProps((nextProps) => {
      if (nextProps.taskId !== this.props.taskId) {
        this.woowProjectAiPaas.info = null;
        if (this.state.activeTab === WOOW_PROJECT_AI_PAAS_TAB) {
          this.state.activeTab = "description";
        }
      }
    });
  },

  async loadTask() {
    // the task this load is for: the props may move to another task while the platform's fetch runs
    const taskId = this.props.taskId;
    await super.loadTask(...arguments);
    if (taskId !== this.props.taskId) {
      return;
    }
    if (!this.state.task || this.state.task.id !== taskId) {
      this.woowProjectAiPaas.info = null;
      return;
    }
    let info;
    try {
      info = await jsonRpc("/woow_project_ai_paas/task_chat", { task_id: taskId });
    } catch {
      info = { available: false };
    }
    if (taskId !== this.props.taskId) {
      return; // another task meanwhile
    }
    this.woowProjectAiPaas.info = info && info.available ? info : { available: false };
    if (!this.woowProjectAiPaas.info.available && this.state.activeTab === WOOW_PROJECT_AI_PAAS_TAB) {
      this.state.activeTab = "description";
    }
  },
});
