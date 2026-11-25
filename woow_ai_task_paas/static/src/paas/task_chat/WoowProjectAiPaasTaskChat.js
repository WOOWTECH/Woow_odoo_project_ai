/** @odoo-module **/
// The customer AI chat of a task on /woow: woow_ai_task's open route in a frame (it creates or joins the chat,
// then shows Odoo's own Discuss page of it), and the same URL in a new tab. The notes are /my's.
import { Component } from "@odoo/owl";

export class WoowProjectAiPaasTaskChat extends Component {
  static template = "woow_ai_task_paas.WoowProjectAiPaasTaskChat";
  static props = {
    info: Object,
  };
}
