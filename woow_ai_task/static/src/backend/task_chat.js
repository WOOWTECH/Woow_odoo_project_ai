/** @odoo-module */
// Task form pages "Internal AI chat" / "Customer AI chat": the conversation of
// the tab, embedded with Odoo's own Discuss components (Thread, Composer).
// Mounted only when its notebook page is shown: opening the page creates the
// conversation the first time, or joins it (project.task.woow_project_ai_open_tab).
// The form keeps this widget when it moves to another task (pager, next
// record): the conversation follows the record, shown at once when the user
// is already a member, else behind an "Open" button (paging through tasks
// never creates or joins conversations).
import {
    Component,
    onWillDestroy,
    onWillStart,
    onWillUpdateProps,
    useRef,
    useState,
    useSubEnv,
} from "@odoo/owl";

import { Composer } from "@mail/core/common/composer";
import { Thread } from "@mail/core/common/thread";
import { Typing } from "@mail/discuss/typing/common/typing";
import { useMessageEdition, useMessageHighlight, useMessageToReplyTo } from "@mail/utils/common/hooks";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";

export class WoowProjectAiTaskChat extends Component {
    static template = "woow_ai_task.TaskChat";
    static components = { Composer, Thread, Typing };
    static props = {
        ...standardWidgetProps,
        tab: { type: String },
    };

    setup() {
        super.setup();
        // the form view gives its children the record chatter's env.chatter: its
        // "fetchMessages" flag (already used by the task's own chatter) would keep
        // the Thread from loading this conversation's messages. messageHighlight:
        // jumping to a quoted or pinned message, as in Discuss and chat windows
        this.messageHighlight = useMessageHighlight();
        useSubEnv({ chatter: undefined, messageHighlight: this.messageHighlight });
        this.store = useState(useService("mail.store"));
        this.orm = useService("orm");
        this.messageEdition = useMessageEdition();
        this.messageToReplyTo = useMessageToReplyTo();
        this.contentRef = useRef("content");
        // autofocus: a counter, as in chat windows (Reply, emoji and edit focus the input again); not on open,
        // which would scroll the form to the composer
        this.state = useState({
            thread: null,
            readonly: false,
            error: "",
            ask: false,
            jumpPresent: 0,
            autofocus: 0,
        });
        this.customerPlaceholder = _t("Message to the customer: the AI pauses after you write…");
        this.loadId = 0;
        // the user opened the page: open (create / join) the conversation; not awaited, so
        // "Opening the AI chat…" shows meanwhile
        onWillStart(() => {
            this.load(this.props);
        });
        onWillUpdateProps((nextProps) => {
            if (nextProps.record.resId !== this.props.record.resId || nextProps.tab !== this.props.tab) {
                this.follow(nextProps);
            }
        });
        // also when destroyed before being mounted
        onWillDestroy(() => {
            this.loadId++;
            this.release();
        });
    }

    /** The shown conversation is no longer embedded here (its chat window comes back). */
    release() {
        if (this.state.thread) {
            this.state.thread.woowProjectAiEmbedded = false;
        }
    }

    /** Another task in the same form: its conversation if already a member, else the "Open" button. */
    async follow(props) {
        const loadId = ++this.loadId;
        this.release();
        Object.assign(this.state, { thread: null, readonly: false, error: "", ask: false });
        const resId = props.record.resId;
        if (!resId) {
            return;
        }
        try {
            const info = await this.orm.silent.call("project.task", "woow_project_ai_tab_info", [
                [resId],
                props.tab,
            ]);
            if (loadId !== this.loadId) {
                return;
            }
            if (info.member) {
                await this.load(props);
            } else {
                this.state.ask = true;
            }
        } catch (error) {
            if (loadId === this.loadId) {
                this.state.error =
                    error?.data?.message || error?.message || _t("The AI chat could not be opened.");
            }
        }
    }

    onClickOpen() {
        this.load(this.props);
    }

    async load(props) {
        const loadId = ++this.loadId;
        this.release();
        Object.assign(this.state, { thread: null, readonly: false, error: "", ask: false });
        const resId = props.record.resId;
        if (!resId) {
            return;
        }
        try {
            const result = await this.orm.silent.call("project.task", "woow_project_ai_open_tab", [
                [resId],
                props.tab,
            ]);
            if (loadId !== this.loadId) {
                return; // another task (or tab) was opened meanwhile
            }
            const thread = await this.store.Thread.getOrFetch({
                model: "discuss.channel",
                id: result.channel_id,
            });
            if (loadId !== this.loadId) {
                return;
            }
            if (!thread) {
                throw new Error(_t("The AI chat could not be opened."));
            }
            // shown here: displayed (no web push, no chat window for its new messages: thread_model_patch.js)
            thread.woowProjectAiEmbedded = true;
            this.state.readonly = result.readonly;
            this.state.thread = thread;
            // opening the page reads the conversation (Discuss marks it read when shown too)
            thread.markAsRead();
        } catch (error) {
            if (loadId === this.loadId) {
                this.state.error =
                    error?.data?.message || error?.message || _t("The AI chat could not be opened.");
            }
        }
    }

    onPosted() {
        this.state.jumpPresent++;
    }
}

registry.category("view_widgets").add("woow_project_ai_task_chat", {
    component: WoowProjectAiTaskChat,
    extractProps: ({ options }) => ({ tab: options.tab || "internal" }),
});
