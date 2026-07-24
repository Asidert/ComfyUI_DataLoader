import { app } from "../../scripts/app.js";

// UI for the DataLoader node.
//
// Instead of hand-editing a JSON array, the user clicks "Add File" and gets a
// row of three fields (destination, source, headers/token). The rows are
// serialised into the hidden `commands` STRING widget that the Python node
// reads. After a run, an on-node panel shows per-file status.

const PH_DEST = "models/loras/x.safetensors";
const PH_SRC = "https://host/x.safetensors";
const PH_HDR = 'token or {"Header": "value"}  (optional)';

const ACCENT = "#2c5c4b";

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
function hideWidget(w) {
    if (!w) return;
    w.hidden = true;
    w.type = "hidden";
    w.computeSize = () => [0, -4];
    if (w.element) w.element.style.display = "none";
    if (w.inputEl) w.inputEl.style.display = "none";
}

function mkInput(placeholder) {
    const i = document.createElement("input");
    i.type = "text";
    i.placeholder = placeholder;
    i.spellcheck = false;
    i.style.cssText =
        "width:100%;box-sizing:border-box;background:#181818;color:#eee;" +
        "border:1px solid #444;border-radius:3px;padding:3px 6px;" +
        "font:11px ui-monospace,Menlo,Consolas,monospace;outline:none;";
    return i;
}

// A headers field value becomes a proper object: either the JSON the user typed,
// or, if it's a bare token, {"Authorization": "Bearer <token>"}.
function parseHeaders(raw) {
    const v = raw.trim();
    if (!v) return null;
    try {
        const obj = JSON.parse(v);
        if (obj && typeof obj === "object" && !Array.isArray(obj)) return obj;
    } catch (e) {
        /* not JSON - treat as a bearer token below */
    }
    return { Authorization: `Bearer ${v}` };
}

function headersToField(headers) {
    if (!headers || typeof headers !== "object") return "";
    const keys = Object.keys(headers);
    if (keys.length === 1 && keys[0] === "Authorization") {
        const m = /^Bearer\s+(.+)$/.exec(headers.Authorization || "");
        if (m) return m[1]; // show just the token we wrapped earlier
    }
    return JSON.stringify(headers);
}

// ---------------------------------------------------------------------------
// editor
// ---------------------------------------------------------------------------
function syncCommands(node) {
    const items = [];
    for (const row of [...node._dlRows.children]) {
        const [dest, src, hdr] = row._fields;
        const d = dest.value.trim();
        const s = src.value.trim();
        const h = hdr.value.trim();
        if (!d && !s && !h) continue;
        const item = [d, s];
        const headers = parseHeaders(h);
        if (headers) item.push(headers);
        items.push(item);
    }
    if (node._dlCommands) node._dlCommands.value = JSON.stringify(items);
}

function addRow(node, data) {
    const row = document.createElement("div");
    row.style.cssText =
        "display:flex;flex-direction:column;gap:4px;padding:6px;" +
        "border:1px solid #3a3a3a;border-radius:5px;background:#202020;";

    const dest = mkInput(PH_DEST);
    const src = mkInput(PH_SRC);
    const hdr = mkInput(PH_HDR);
    hdr.style.display = "none"; // hidden until "Add Headers"

    // "Add Headers" button occupies the header slot until clicked.
    const addHdrBtn = document.createElement("button");
    addHdrBtn.textContent = "+ Add Headers";
    addHdrBtn.style.cssText =
        "align-self:flex-start;cursor:pointer;padding:2px 8px;border-radius:3px;" +
        "border:1px solid #4a4a4a;background:#252525;color:#bbb;font:11px sans-serif;";
    const showHeaders = () => {
        hdr.style.display = "";
        addHdrBtn.style.display = "none";
    };
    addHdrBtn.onclick = (e) => {
        e.preventDefault();
        showHeaders();
        hdr.focus();
        syncCommands(node);
        resize(node);
    };

    if (data) {
        dest.value = data.destination || "";
        src.value = data.source || "";
        hdr.value = headersToField(data.headers);
        if (hdr.value) showHeaders(); // auto-reveal when a saved row has headers
    }

    const del = document.createElement("button");
    del.textContent = "Remove";
    del.style.cssText =
        "align-self:flex-end;cursor:pointer;padding:2px 8px;border-radius:3px;" +
        "border:1px solid #5a3a3a;background:#3a2020;color:#f0a0a0;font:11px sans-serif;";
    del.onclick = (e) => {
        e.preventDefault();
        row.remove();
        syncCommands(node);
        resize(node);
    };

    for (const el of [dest, src, hdr]) {
        el.addEventListener("input", () => syncCommands(node));
    }

    row._fields = [dest, src, hdr];
    row.append(dest, src, hdr, addHdrBtn, del);
    node._dlRows.appendChild(row);
    return row;
}

function buildEditor(node) {
    const wrap = document.createElement("div");
    wrap.style.cssText =
        "display:flex;flex-direction:column;gap:6px;width:100%;" +
        "box-sizing:border-box;padding:2px 0;";

    const rows = document.createElement("div");
    rows.style.cssText = "display:flex;flex-direction:column;gap:6px;";
    node._dlRows = rows;
    wrap.appendChild(rows);

    const addBtn = document.createElement("button");
    addBtn.textContent = "+ Add File";
    addBtn.style.cssText =
        "cursor:pointer;padding:5px 8px;border-radius:4px;border:1px solid #555;" +
        `background:${ACCENT};color:#fff;font:12px sans-serif;font-weight:600;`;
    addBtn.onclick = (e) => {
        e.preventDefault();
        addRow(node);
        syncCommands(node);
        resize(node);
    };
    wrap.appendChild(addBtn);

    node._dlWrap = wrap;
    return wrap;
}

function rebuildRows(node) {
    if (!node._dlRows) return;
    node._dlRows.innerHTML = "";
    let items = [];
    try {
        items = JSON.parse(node._dlCommands?.value || "[]");
    } catch (e) {
        items = [];
    }
    if (!Array.isArray(items) || items.length === 0) {
        addRow(node); // start with one empty row
    } else {
        for (const it of items) {
            if (Array.isArray(it)) {
                addRow(node, { destination: it[0], source: it[1], headers: it[2] });
            } else if (it && typeof it === "object") {
                addRow(node, {
                    destination: it.destination || it.dest || it.to,
                    source: it.source || it.url || it.from,
                    headers: it.headers,
                });
            }
        }
    }
    resize(node);
}

function resize(node) {
    requestAnimationFrame(() => {
        const w = Math.max(node.size[0], 340);
        node.setSize([w, node.computeSize()[1]]);
        node.setDirtyCanvas(true, true);
    });
}

// ---------------------------------------------------------------------------
// result panel (after execution)
// ---------------------------------------------------------------------------
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
                if (it.ok === false) return `✗ ${name} — ${it.error || "error"}`;
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
        const rw = node.addDOMWidget("dl_result", "div", el, { serialize: false });
        rw.computeSize = (width) => [width, (el.scrollHeight || 20) + 4];
    }
    node._dlResultEl.textContent = display;
    resize(node);
}

// ---------------------------------------------------------------------------
// registration
// ---------------------------------------------------------------------------
app.registerExtension({
    name: "Data.DataLoader",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "DataLoader") return;

        nodeType.title_color = ACCENT;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
            this.color = ACCENT;

            this._dlCommands = this.widgets?.find((w) => w.name === "commands");
            hideWidget(this._dlCommands);

            const wrap = buildEditor(this);
            const ew = this.addDOMWidget("dl_editor", "div", wrap, { serialize: false });
            ew.computeSize = (width) => [width, (wrap.scrollHeight || 60) + 4];

            rebuildRows(this); // one empty row for a fresh node
            return r;
        };

        // Rebuild rows from the saved `commands` value when a workflow loads.
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const r = onConfigure ? onConfigure.apply(this, arguments) : undefined;
            rebuildRows(this);
            return r;
        };

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            const r = onExecuted ? onExecuted.apply(this, arguments) : undefined;
            renderResult(this, message && message.text && message.text[0]);
            return r;
        };
    },
});
