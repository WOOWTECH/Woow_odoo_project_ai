/** @odoo-module **/
// /woow project kanban: while the old task AI is off, its "AI" and "Agent" status pills are hidden
// (kanban_patch.xml) and their three status calls are never made (they would be refused anyway).
import { patch } from "@web/core/utils/patch";
import { ProjectKanbanPage } from "@woow_paas_platform/paas/pages/project-kanban/ProjectKanbanPage";
import { legacyTaskAi } from "./legacy_flag";

patch(ProjectKanbanPage.prototype, {
  setup() {
    super.setup(...arguments);
    this.woowProjectAiPaasLegacy = legacyTaskAi();
  },

  async _loadStatuses() {
    if (!this.woowProjectAiPaasLegacy) {
      return;
    }
    return super._loadStatuses(...arguments);
  },
});
