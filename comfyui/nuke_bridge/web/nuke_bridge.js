// Nuke bridge frontend extension.
//
// Publishes the currently open ComfyUI workflow to the backend
// /nuke_bridge/workflows so Nuke can list it in its dropdown. Also keeps the
// listing fresh while the tab is open. Nuke triggers execution via the
// backend; this script only advertises workflows.
//
// Robustness policy: every ComfyUI/litegraph access is wrapped in try/catch.
// The extension degrades silently if any internal API moves between versions.

import { app } from "/scripts/app.js";

const FROM_NUKE_TYPES = ["FromNuke", "FromNukeVideo", "Nuke Bridge: From Nuke", "Nuke Bridge: From Nuke Video"];
const TO_NUKE_TYPES = ["ToNuke", "ToNukeVideo", "Nuke Bridge: To Nuke", "Nuke Bridge: To Nuke Video"];
const BRIDGE_TYPES = [...FROM_NUKE_TYPES, ...TO_NUKE_TYPES];
const PUBLISH_DEBOUNCE_MS = 800;
const POLL_MS = 3000;

// Per-tab stable id stored in sessionStorage so reloads keep the same slot.
let tabId = sessionStorage.getItem("nuke_bridge_tab_id");
if (!tabId) {
    tabId = "wf-" + Math.random().toString(36).slice(2, 10);
    sessionStorage.setItem("nuke_bridge_tab_id", tabId);
}

let publishTimer = null;

function safe(fn, fallback) {
    try { return fn(); } catch (_) { return fallback; }
}

function nodeTypeValues(node) {
    if (!node) return false;
    const values = [];
    for (const key of ["type", "comfyClass", "title"]) {
        const value = safe(() => node[key], null);
        if (value) values.push(String(value));
    }
    const data = safe(() => node.data || node.nodeData, null);
    if (data) {
        for (const key of ["type", "name", "display_name"]) {
            const value = safe(() => data[key], null);
            if (value) values.push(String(value));
        }
    }
    const props = safe(() => node.properties, null);
    if (props) {
        for (const key of ["class_type", "Node name for S&R"]) {
            const value = safe(() => props[key], null);
            if (value) values.push(String(value));
        }
    }
    return values;
}

function nodeMatchesAnyType(node, typeList) {
    const values = nodeTypeValues(node);
    return values && values.some(v => typeList.includes(v));
}

function collectNodes() {
    const g = app.graph;
    if (!g) return [];
    let nodes = safe(() => g._nodes, null);
    if (nodes && nodes.length) return nodes;
    nodes = safe(() => g.nodes, null);
    if (nodes) {
        if (Array.isArray(nodes)) return nodes;
        // plain object map -> values
        try { return Object.values(nodes); } catch (_) { return []; }
    }
    return [];
}

function bridgeIdWidget(node) {
    return safe(() => (node.widgets || []).find(w => w && w.name === "bridge_id"), null);
}

function newBridgeId() {
    const uuid = safe(() => crypto.randomUUID().replaceAll("-", ""), null);
    const token = uuid || (Date.now().toString(36) + Math.random().toString(36).slice(2));
    return "bridge-" + token.slice(0, 16);
}

function ensureBridgeId(node) {
    if (!nodeMatchesAnyType(node, BRIDGE_TYPES)) return;
    queueMicrotask(() => {
        const widget = bridgeIdWidget(node);
        if (!widget) return;
        const current = String(widget.value || "").trim();
        const duplicate = current && collectNodes().some(other =>
            other !== node && String(bridgeIdWidget(other)?.value || "").trim() === current
        );
        if (current && !duplicate) return;
        widget.value = newBridgeId();
        safe(() => node.graph.setDirtyCanvas(true, true), null);
        debouncedPublish();
    });
}

function workflowName() {
    const g = app.graph;
    const fromGraph = safe(() => g && g.name, null);
    if (fromGraph) return String(fromGraph);
    return safe(() => document.title, null) || "Workflow";
}

async function buildPayload() {
    let hasFrom = false;
    let hasTo = false;
    for (const n of collectNodes()) {
        if (nodeMatchesAnyType(n, FROM_NUKE_TYPES)) hasFrom = true;
        if (nodeMatchesAnyType(n, TO_NUKE_TYPES)) hasTo = true;
    }

    let prompt = null;
    try {
        const out = await app.graphToPrompt();
        prompt = (out && (out.output || out.prompt)) || out;
    } catch (_) {
        prompt = null; // graph not runnable as-is; still advertise the entry
    }

    return {
        workflows: [{
            id: tabId,
            name: workflowName(),
            has_from_nuke: hasFrom,
            has_to_nuke: hasTo,
            prompt: prompt,
        }],
    };
}

async function publish() {
    try {
        const payload = await buildPayload();
        await fetch("/nuke_bridge/workflows", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
    } catch (_) {
        // best effort; the next tick/poll will retry
    }
}

function debouncedPublish() {
    if (publishTimer) clearTimeout(publishTimer);
    publishTimer = setTimeout(publish, PUBLISH_DEBOUNCE_MS);
}

function wireGraphHooks() {
    const g = app.graph;
    if (!g) return false;
    // ponytail: wrap a handful of litegraph callbacks; if any is missing or
    // has a different shape, the try/catch keeps us alive.
    for (const key of ["onNodeAdded", "onNodeRemoved", "onConnectionChange", "onGraphChanged"]) {
        try {
            const orig = g[key];
            g[key] = function () {
                try { if (orig) orig.apply(this, arguments); } catch (_) {}
                debouncedPublish();
            };
        } catch (_) {
            // property may be non-writable; skip
        }
    }
    return true;
}

app.registerExtension({
    name: "NukeBridge.WorkflowPublisher",
    nodeCreated(node) {
        ensureBridgeId(node);
    },
    async setup() {
        // Graph may not be ready at setup; retry briefly.
        if (!wireGraphHooks()) {
            let tries = 0;
            const iv = setInterval(() => {
                if (wireGraphHooks() || ++tries > 20) clearInterval(iv);
            }, 500);
        }

        publish();
        // Periodic re-publish as a safety net (also refreshes the backend
        // timestamp so the entry doesn't expire while the tab is open).
        setInterval(publish, POLL_MS);

        // Re-publish after ComfyUI finishes a prompt build / load.
        try {
            const origLoad = app.loadGraphData;
            if (typeof origLoad === "function") {
                app.loadGraphData = function () {
                    const p = origLoad.apply(this, arguments);
                    try { Promise.resolve(p).finally(debouncedPublish); } catch (_) {}
                    return p;
                };
            }
        } catch (_) {}
    },
});
