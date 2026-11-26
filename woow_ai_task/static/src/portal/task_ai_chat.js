/** @odoo-module */
// Portal task page: the customer AI chat is loaded on demand (opening it
// creates or joins the conversation), then shown embedded. Never with the
// page itself: the Discuss page in the frame focuses its composer, which
// would scroll the task page down on every visit. A member who came through
// the section's anchor (#woow_project_ai_chat) gets it at once. The button is
// a link to the full page (new tab): it still works before this script starts.
import publicWidget from "@web/legacy/js/public/public_widget";

publicWidget.registry.WoowProjectAiPortalChat = publicWidget.Widget.extend({
    selector: ".o_woow_project_ai_portal",
    events: {
        "click .o_woow_project_ai_portal_open": "_onOpen",
    },

    start() {
        const result = this._super(...arguments);
        if (this.el.dataset.member === "1" && window.location.hash === "#woow_project_ai_chat") {
            this._show();
        }
        return result;
    },

    _onOpen(ev) {
        ev.preventDefault();
        this._show();
    },

    _show() {
        const frame = this.el.querySelector(".o_woow_project_ai_portal_frame");
        if (frame && !frame.getAttribute("src")) {
            frame.setAttribute("src", frame.dataset.src);
        }
        frame?.classList.remove("d-none");
        this.el.querySelector(".o_woow_project_ai_portal_start")?.classList.add("d-none");
    },
});
