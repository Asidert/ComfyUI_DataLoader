import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// UI for the DataLoader node:
//  - "Add File" rows (destination / source / optional headers) that serialise
//    into the hidden `commands` widget the Python node reads.
//  - a live progress panel: one bar per file, driven by WebSocket events the
//    node emits while downloading.
//  - a ResizeObserver keeps the node height in sync with its DOM content.

const PH_DEST = "models/loras/x.safetensors";
const PH_SRC = "https://host/x.safetensors";
const PH_HDR = 'token or {"Header": "value"}  (optional)';
const ACCENT = "#2c5c4b";
const ACCENT_LIGHT = "#3a7a63";
const MIN_W = 340;

// ---------------------------------------------------------------------------
// one-time stylesheet
// ---------------------------------------------------------------------------
(function injectStyle() {
    if (document.getElementById("dl-style")) return;
    const s = document.createElement("style");
    s.id = "dl-style";
    s.textContent = `
.dl-track{position:relative;height:9px;background:#333;border-radius:5px;overflow:hidden;}
.dl-fill{position:absolute;left:0;top:0;height:100%;width:0;background:${ACCENT_LIGHT};
         border-radius:5px;transition:width .15s linear;}
.dl-fill.cached{background:#6a6a6a;}
.dl-fill.err{background:#a53a3a;}
.dl-fill.ind{width:35%;transition:none;animation:dl-slide 1.1s linear infinite;}
@keyframes dl-slide{0%{transform:translateX(-120%);}100%{transform:translateX(320%);}}`;
    document.head.appendChild(s);
})();

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------
function getNode(id) {
    return app.graph && app.graph.getNodeById ? app.graph.getNodeById(Number(id)) : null;
}

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

function parseHeaders(raw) {
    const v = raw.trim();
    if (!v) return null;
    try {
        const obj = JSON.parse(v);
        if (obj && typeof obj === "object" && !Array.isArray(obj)) return obj;
    } catch (e) {
        /* not JSON - treat as a bearer token */
    }
    return { Authorization: `Bearer ${v}` };
}

function headersToField(headers) {
    if (!headers || typeof headers !== "object") return "";
    const keys = Object.keys(headers);
    if (keys.length === 1 && keys[0] === "Authorization") {
        const m = /^Bearer\s+(.+)$/.exec(headers.Authorization || "");
        if (m) return m[1];
    }
    return JSON.stringify(headers);
}

// ---------------------------------------------------------------------------
// sizing (ResizeObserver-driven, with a guard against feedback loops)
// ---------------------------------------------------------------------------
function resize(node) {
    requestAnimationFrame(() => {
        if (!node.graph) return;
        const target = node.computeSize();
        const w = Math.max(node.size[0], MIN_W);
        if (Math.abs(node.size[1] - target[1]) > 1 || node.size[0] < MIN_W) {
            node.setSize([w, target[1]]);
            node.setDirtyCanvas(true, true);
        }
    });
}

function observe(node, el) {
    if (!node._dlRO) node._dlRO = new ResizeObserver(() => resize(node));
    node._dlRO.observe(el);
}

// ---------------------------------------------------------------------------
// command editor
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
    hdr.style.display = "none";

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
        if (hdr.value) showHeaders();
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
    const outer = document.createElement("div");
    outer.style.cssText = "width:100%;box-sizing:border-box;";

    const content = document.createElement("div");
    content.style.cssText =
        "display:flex;flex-direction:column;gap:6px;width:100%;" +
        "box-sizing:border-box;padding:2px 0;";
    outer.appendChild(content);

    const rows = document.createElement("div");
    rows.style.cssText = "display:flex;flex-direction:column;gap:6px;";
    node._dlRows = rows;
    content.appendChild(rows);

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
    content.appendChild(addBtn);

    node._dlEditorContent = content;
    return outer;
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
        addRow(node);
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

// ---------------------------------------------------------------------------
// progress panel
// ---------------------------------------------------------------------------
function ensureProgressPanel(node) {
    if (node._dlProgContent) return;
    const outer = document.createElement("div");
    outer.style.cssText = "width:100%;box-sizing:border-box;";
    const content = document.createElement("div");
    content.style.cssText =
        "display:flex;flex-direction:column;gap:8px;box-sizing:border-box;padding:2px 0;";
    outer.appendChild(content);
    node._dlProgContent = content;
    const w = node.addDOMWidget("dl_progress", "div", outer, { serialize: false });
    w.computeSize = (width) => [width, (content.offsetHeight || 0) + (content.children.length ? 6 : 0)];
    observe(node, content);
}

function buildBars(node, files, overall) {
    ensureProgressPanel(node);
    const content = node._dlProgContent;
    content.innerHTML = "";
    node._dlBars = {};
    node._dlOverall = null;

    if (overall && overall.files_total > 0) {
        const wrap = document.createElement("div");
        wrap.style.cssText = "display:flex;flex-direction:column;gap:3px;" +
            "padding-bottom:6px;margin-bottom:2px;border-bottom:1px solid #3a3a3a;";
        const top = document.createElement("div");
        top.style.cssText =
            "display:flex;justify-content:space-between;gap:8px;" +
            "font:11px ui-monospace,Menlo,Consolas,monospace;color:#dfe;";
        const name = document.createElement("span");
        name.textContent = "Overall";
        name.style.cssText = "font-weight:700;color:#cfe8de;";
        const pct = document.createElement("span");
        pct.textContent = `0/${overall.files_total} files`;
        pct.style.cssText = "flex:none;color:#9fbfb2;text-align:right;";
        top.append(name, pct);
        const track = document.createElement("div");
        track.className = "dl-track";
        track.style.height = "11px";
        const fill = document.createElement("div");
        fill.className = "dl-fill";
        track.appendChild(fill);
        const probe = document.createElement("div");
        probe.style.cssText = "font:10px sans-serif;color:#c8a86a;display:none;";
        wrap.append(top, track, probe);
        content.appendChild(wrap);
        node._dlOverall = { fill, pct, probe };
    }

    for (const f of files || []) {
        const wrap = document.createElement("div");
        wrap.style.cssText = "display:flex;flex-direction:column;gap:3px;";

        const top = document.createElement("div");
        top.style.cssText =
            "display:flex;justify-content:space-between;gap:8px;" +
            "font:11px ui-monospace,Menlo,Consolas,monospace;color:#ccc;";
        const name = document.createElement("span");
        name.textContent = f.name || `file ${f.index}`;
        name.style.cssText =
            "flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
        const pct = document.createElement("span");
        pct.textContent = "0%";
        pct.style.cssText = "flex:none;color:#9fbfb2;";
        top.append(name, pct);

        const track = document.createElement("div");
        track.className = "dl-track";
        const fill = document.createElement("div");
        fill.className = "dl-fill";
        track.appendChild(fill);

        const err = document.createElement("div");
        err.style.cssText = "font:10px sans-serif;color:#e08a8a;display:none;";

        wrap.append(top, track, err);
        content.appendChild(wrap);
        node._dlBars[f.index] = { fill, pct, err };
    }
    resize(node);
}

function fmtSpeed(bps) {
    if (!bps || bps <= 0) return "";
    if (bps >= 1048576) return (bps / 1048576).toFixed(1) + " MB/s";
    if (bps >= 1024) return (bps / 1024).toFixed(0) + " KB/s";
    return Math.round(bps) + " B/s";
}

function fmtEta(s) {
    if (s == null || !isFinite(s) || s < 0) return "";
    s = Math.round(s);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

function onProgress(node, index, done, total, speed, eta) {
    const bar = node._dlBars && node._dlBars[index];
    if (!bar) return;
    const sp = fmtSpeed(speed);
    if (total > 0) {
        bar.fill.classList.remove("ind");
        const p = Math.min(100, Math.floor((done / total) * 100));
        bar.fill.style.width = p + "%";
        const et = fmtEta(eta);
        bar.pct.textContent = [p + "%", sp, et && "ETA " + et].filter(Boolean).join(" · ");
    } else {
        bar.fill.classList.add("ind");
        bar.pct.textContent = [(done / 1048576).toFixed(0) + " MiB", sp]
            .filter(Boolean).join(" · ");
    }
}

function onOverall(node, d) {
    const bar = node._dlOverall;
    if (!bar) return;
    const sp = fmtSpeed(d.speed);
    let p, parts;
    if (d.total > 0) {
        p = Math.min(100, Math.floor((d.done / d.total) * 100));
        const et = fmtEta(d.eta);
        parts = [`${d.files_done}/${d.files_total} files`, p + "%", sp, et && "ETA " + et];
    } else {
        p = d.files_total ? Math.floor((d.files_done / d.files_total) * 100) : 0;
        parts = [`${d.files_done}/${d.files_total} files`, sp];
    }
    bar.fill.style.width = p + "%";
    bar.pct.textContent = parts.filter(Boolean).join(" · ");
}

function onSpeedcheck(node, d) {
    const bar = node._dlOverall;
    if (!bar || !bar.probe) return;
    const parts = [`speed check: ${fmtSpeed(d.speed)} over ${d.window_seconds}s`];
    if (d.eta_total != null) parts.push(`est. total ${fmtEta(d.eta_total)}`);
    bar.probe.textContent = parts.join(" · ");
    bar.probe.style.display = "";
    resize(node);
}

function onFile(node, index, status, error) {
    const bar = node._dlBars && node._dlBars[index];
    if (!bar) return;
    bar.fill.classList.remove("ind");
    bar.fill.style.width = "100%";
    if (status === "error") {
        bar.fill.classList.add("err");
        bar.pct.textContent = "error";
        if (error) {
            bar.err.textContent = error;
            bar.err.style.display = "";
        }
    } else if (status === "cached") {
        bar.fill.classList.add("cached");
        bar.pct.textContent = "cached";
    } else {
        bar.pct.textContent = "done";
    }
    resize(node);
}

// Fallback: if WS events were missed, paint terminal states from the summary.
function applySummary(node, text) {
    let items = null;
    try {
        items = JSON.parse(text);
    } catch (e) {
        return;
    }
    if (!Array.isArray(items)) return;
    if (!node._dlBars || Object.keys(node._dlBars).length === 0) {
        buildBars(node, items.map((it, i) => ({
            index: i, name: String(it.destination || "").split("/").pop(),
        })));
    }
    items.forEach((it, i) => {
        if (it.ok === false) onFile(node, i, "error", it.error);
        else onFile(node, i, it.downloaded ? "downloaded" : "cached");
    });
}

// ---------------------------------------------------------------------------
// text note panel (ModelSync summary)
// ---------------------------------------------------------------------------
function showNote(node, text) {
    if (!node._dlNoteEl) {
        const outer = document.createElement("div");
        outer.style.cssText = "width:100%;box-sizing:border-box;";
        const el = document.createElement("div");
        el.style.cssText =
            "background:#181818;color:#dcdcdc;font:11px/1.4 ui-monospace,Menlo,Consolas,monospace;" +
            "padding:6px 8px;border-radius:4px;white-space:pre-wrap;box-sizing:border-box;";
        outer.appendChild(el);
        node._dlNoteEl = el;
        const w = node.addDOMWidget("dl_note", "div", outer, { serialize: false });
        w.computeSize = (width) => [width, (el.offsetHeight || 18) + 6];
        observe(node, el);
    }
    node._dlNoteEl.textContent = text;
    resize(node);
}

function showSyncSummary(node, text) {
    let s = null;
    try {
        s = JSON.parse(text);
    } catch (e) {
        return;
    }
    if (!s || !s.counts) return;
    const c = s.counts;
    let msg = `updated ${c.updated} · up-to-date ${c.up_to_date} · ` +
              `errors ${c.errors}  (total ${c.total})`;
    if (s.errors && s.errors.length) {
        msg += "\n" + s.errors.map((e) => `✗ ${e.target}: ${e.error}`).join("\n");
    }
    showNote(node, msg);
}

// ---------------------------------------------------------------------------
// mode switching (manual <-> manifest): toggle which widgets are visible
// ---------------------------------------------------------------------------
const MANUAL_ONLY = ["overwrite"];
const MANIFEST_ONLY = ["manifest_url", "token", "force"];

function findW(node, name) {
    return node.widgets && node.widgets.find((w) => w.name === name);
}

function setWidgetVisible(node, w, visible) {
    if (!w) return;
    if (w._dlOrigType === undefined) {
        w._dlOrigType = w.type;
        w._dlOrigCompute = w.computeSize;
    }
    if (visible) {
        w.type = w._dlOrigType;
        w.computeSize = w._dlOrigCompute;
        if (w.element) w.element.style.display = "";
        if (w.inputEl) w.inputEl.style.display = "";
    } else {
        w.type = "hidden";
        w.computeSize = () => [0, -4];
        if (w.element) w.element.style.display = "none";
        if (w.inputEl) w.inputEl.style.display = "none";
    }
}

function getMode(node) {
    const w = findW(node, "mode");
    return w && w.value === "manifest" ? "manifest" : "manual";
}

function applyMode(node) {
    const manifest = getMode(node) === "manifest";
    for (const name of MANUAL_ONLY) setWidgetVisible(node, findW(node, name), !manifest);
    for (const name of MANIFEST_ONLY) setWidgetVisible(node, findW(node, name), manifest);
    if (node._dlEditorWidget) setWidgetVisible(node, node._dlEditorWidget, !manifest);
    resize(node);
}

// ---------------------------------------------------------------------------
// registration
// ---------------------------------------------------------------------------
app.registerExtension({
    name: "Data.DataLoader",

    async setup() {
        api.addEventListener("dataloader.start", (e) => {
            const n = getNode(e.detail.node);
            if (n) buildBars(n, e.detail.files, e.detail.overall);
        });
        api.addEventListener("dataloader.overall", (e) => {
            const n = getNode(e.detail.node);
            if (n) onOverall(n, e.detail);
        });
        api.addEventListener("dataloader.speedcheck", (e) => {
            const n = getNode(e.detail.node);
            if (n) onSpeedcheck(n, e.detail);
        });
        api.addEventListener("dataloader.progress", (e) => {
            const n = getNode(e.detail.node);
            if (n) onProgress(n, e.detail.index, e.detail.done, e.detail.total,
                              e.detail.speed, e.detail.eta);
        });
        api.addEventListener("dataloader.file", (e) => {
            const n = getNode(e.detail.node);
            if (n) onFile(n, e.detail.index, e.detail.status, e.detail.error);
        });
    },

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "DataLoader") return;

        nodeType.title_color = ACCENT;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
            const node = this;
            node.color = ACCENT;

            node._dlCommands = findW(node, "commands");
            hideWidget(node._dlCommands);

            const outer = buildEditor(node);
            const ew = node.addDOMWidget("dl_editor", "div", outer, { serialize: false });
            ew.computeSize = (width) =>
                [width, (node._dlEditorContent.offsetHeight || 60) + 6];
            node._dlEditorWidget = ew;
            observe(node, node._dlEditorContent);

            const modeW = findW(node, "mode");
            if (modeW) {
                const orig = modeW.callback;
                modeW.callback = function () {
                    const rr = orig ? orig.apply(this, arguments) : undefined;
                    applyMode(node);
                    return rr;
                };
            }

            rebuildRows(node);
            applyMode(node);
            return r;
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const r = onConfigure ? onConfigure.apply(this, arguments) : undefined;
            rebuildRows(this);
            applyMode(this);
            return r;
        };

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            const r = onExecuted ? onExecuted.apply(this, arguments) : undefined;
            const text = message && message.text && message.text[0];
            if (text) {
                if (getMode(this) === "manifest") showSyncSummary(this, text);
                else applySummary(this, text);
            }
            resize(this);
            return r;
        };

        const onRemoved = nodeType.prototype.onRemoved;
        nodeType.prototype.onRemoved = function () {
            if (this._dlRO) {
                this._dlRO.disconnect();
                this._dlRO = null;
            }
            return onRemoved ? onRemoved.apply(this, arguments) : undefined;
        };
    },
});
