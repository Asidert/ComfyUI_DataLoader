import { app } from "../../scripts/app.js";

// UI for the DataLoader node: a friendly editor for the command list and an
// on-node result panel that shows per-file download status after execution.

const PLACEHOLDER =
    '[\n  ["models/loras/x.safetensors", "https://host/x.safetensors"],\n' +
    '  ["models/loras/y.safetensors", "https://host/y.safetensors", {"Authorization": "Bearer TOKEN"}]\n]';

const TITLE_COLOR = "#2c5c4b";
const TITLE_COLOR_LIGHT = "#3a7a63";

function renderResult(node, text) {
    if (!text) return;

    let items = null;
    try {
        items = JSON.parse(text);
    } catch (e) {
        items = null;
    }

    let display;
    if (Array.isArray(items)) {
        display = items
            .map((it) => {
                const name = String(it.destination || "").split("/").pop();
                if (it.ok === false) {
                    return `✗ ${name} — ${it.error || "error"}`;
                }
                return it.downloaded ? `↓ ${name}` : `• ${name} (cached)`;
            })
            .join("\n");
    } else {
        display = text;
    }

    if (!node._dlResultEl) {
        const el = document.createElement("div");
        el.style.cssText =
            "background:#181818;color:#dcdcdc;font:11px/1.4 ui-monospace,Menlo,Consolas,monospace;" +
            "padding:6px 8px;border-radius:4px;white-space:pre-wrap;overflow:auto;box-sizing:border-box;";
        node._dlResultEl = el;
        node.addDOMWidget("dataloader_result", "div", el, { serialize: false });
    }
    node._dlResultEl.textContent = display;
    node.setDirtyCanvas(true, true);
}

app.registerExtension({
    name: "FlammaData.DataLoader",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "DataLoader") return;

        nodeType.title_color = TITLE_COLOR;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;

            this.color = TITLE_COLOR;
            this.bgcolor = TITLE_COLOR_LIGHT;

            const w = this.widgets && this.widgets.find((x) => x.name === "commands");
            if (w && w.inputEl) {
                w.inputEl.placeholder = PLACEHOLDER;
                w.inputEl.style.fontFamily =
                    "ui-monospace,Menlo,Consolas,monospace";
                w.inputEl.style.fontSize = "11px";
                w.inputEl.spellcheck = false;
            }

            if (this.size[0] < 380) this.size[0] = 380;
            return r;
        };

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            const r = onExecuted ? onExecuted.apply(this, arguments) : undefined;
            const text = message && message.text && message.text[0];
            renderResult(this, text);
            return r;
        };
    },
});
