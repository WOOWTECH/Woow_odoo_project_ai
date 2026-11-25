/** @odoo-module **/
// Whether the old platform task AI is switched on (woow_ai_task_paas.legacy_task_ai): the /woow page reads
// the flag the server put in the session info. Read through @web/session (the bare odoo.__session_info__ is
// deleted after boot, see the platform's core/platform_config.js); fail-closed: missing or not true -> off.
import { session } from "@web/session";

export function legacyTaskAi() {
  return session.woow_project_ai_paas_legacy === true;
}
