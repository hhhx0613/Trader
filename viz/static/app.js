/* 研究链路可视化调试台前端：无构建、无框架，只走本机 /api。
 *
 * 数据来源两条：
 *   1) 运行中的事件流（SSE，/api/stream）——阶段流转、每个 Agent 节点的输入/工具/输出、对象产出
 *   2) 账本只读查询（/api/object、/api/trace、/api/raw）——点任意 ID 溯源到持久化事实与正文
 * 页面刷新后事件流不在内存时，可从「账本回放 → 历史运行」按 JSONL 重放。
 */

const NODE_ORDER = ["event", "fundamental", "market", "risk_critic", "committee", "packet"];
// 逐标的研究图里应当出现的节点：某节点可能一次模型都不调就弃权（见 SILENT_HINTS），
// 前端仍要把它排进流水线，否则用户会误以为链路缺了一段。
const GRAPH_NODES = ["event", "fundamental", "market", "risk_critic"];
const SILENT_HINTS = {
  fundamental: "无模型调用：Event 未立论，或本 Snapshot 无 filing 证据（跑全程会采集 SEC filings；若该 Snapshot 无披露卡则本节点干净弃权）",
  event: "无模型调用：注入证据为空，节点直接弃权",
  market: "无模型调用：行情类证据为空",
  risk_critic: "无模型调用：上游无 ClaimCard 时不进入审计",
};
const KIND_LABEL = {
  snapshot: "Snapshot",
  evidence_card: "EvidenceCard",
  claim_card: "ClaimCard",
  research_packet: "ResearchPacket",
  thesis_book: "ThesisBook",
  portfolio_intent: "PortfolioIntent",
  intent_constraints: "IntentConstraints",
  target_portfolio: "TargetPortfolio",
  risk_projected_portfolio: "RiskProjectedPortfolio",
  order_plan: "OrderPlan",
  raw_record: "采集记录",
  unknown: "对象",
};

const state = {
  meta: null,
  pool: new Set(["NVDA"]),
  runId: null,
  running: false,
  events: [],
  stages: {},
  calls: new Map(),
  order: [],
  objects: new Map(),
  tab: "stream",
  stageFilter: null,
  search: "",
  expanded: new Set(),
  es: null,
};

/* ---------------- 小工具 ---------------- */

const $ = (sel) => document.querySelector(sel);
const HIDDEN_KEYS = ["data", "prompt", "output"];

function h(tag, opts = {}, children) {
  const node = document.createElement(tag);
  if (typeof opts === "string") node.className = opts;
  else {
    Object.entries(opts).forEach(([k, v]) => {
      if (v == null) return;
      if (k === "cls") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "on") Object.entries(v).forEach(([ev, fn]) => node.addEventListener(ev, fn));
      else node.setAttribute(k, v);
    });
  }
  (Array.isArray(children) ? children : children ? [children] : []).forEach((c) => c && node.append(c));
  return node;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const text = await res.text();
  let body;
  try { body = text ? JSON.parse(text) : {}; } catch { body = { raw: text }; }
  if (!res.ok) throw new Error((body && (body.error || body.detail)) || `HTTP ${res.status}`);
  return body;
}

const shortId = (id) => (id ? String(id).replace(/^(ev|cl|rp|tb|pi|snap|viz)_/, "").slice(0, 10) : "—");
const clip = (value, n = 90) => {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  if (!text) return "";
  return text.length > n ? text.slice(0, n) + "…" : text;
};
const fmtTime = (ts) => (ts ? new Date(ts).toLocaleTimeString("zh-CN", { hour12: false }) : "");
const num = (value) => (typeof value === "number" ? (Number.isInteger(value) ? value : value.toFixed(3)) : value);

function jsonBlock(obj, label = "完整 JSON") {
  return h("details", {}, [h("summary", { text: label }),
    h("pre", { cls: "json", text: JSON.stringify(obj, null, 2) })]);
}

function idChip(kind, id, label) {
  if (!id) return h("span", { cls: "muted", text: "—" });
  return h("span", {
    cls: "id-chip", text: label || shortId(id), title: id,
    on: { click: () => openDrawer(kind, id) },
  });
}

function badge(kind, label) {
  return h("span", { cls: `badge ${kind}`, text: label || kind });
}

/* ---------------- 初始化 ---------------- */

async function init() {
  state.meta = await api("/api/meta");
  buildPool();
  buildSources();
  buildProviders();
  $("#accountState").value = JSON.stringify(state.meta.default_account, null, 2);
  renderMetaInfo();
  bindUI();
  loadSnapshots();
  renderAll();
}

function renderMetaInfo() {
  const box = $("#metaInfo");
  box.textContent = "";
  const v = state.meta.versions;
  const p = state.meta.paths;
  [["news", v.news], ["market", v.market], ["filings", v.filings],
   ["ledger", p.ledger], ["正文库", p.raw], ["运行流", p.runs], ["LLM 日志", p.llm_log]]
    .forEach(([k, val]) => box.append(h("b", { text: k }), h("span", { cls: "mono", text: String(val) })));
}

function buildPool() {
  const grid = $("#poolGrid");
  grid.textContent = "";
  state.meta.pool.forEach((symbol) => {
    grid.append(h("button", {
      cls: `chip-toggle${state.pool.has(symbol) ? " on" : ""}`, text: symbol,
      on: { click: () => { state.pool.has(symbol) ? state.pool.delete(symbol) : state.pool.add(symbol); buildPool(); } },
    }));
  });
  $("#poolCount").textContent = state.pool.size;
}

function buildSources() {
  const box = $("#sourceRadios");
  box.textContent = "";
  state.meta.run_modes.forEach((mode, index) => {
    box.append(h("label", { cls: "check" }, [
      h("input", { type: "radio", name: "source", value: mode.value, ...(index === 0 ? { checked: "" } : {}),
        on: { change: () => $("#snapshotPick").classList.toggle("hidden", mode.value !== "snapshot") } }),
      h("span", { text: mode.label }),
    ]));
  });
  $("#snapshotPick").classList.add("hidden");
}

function buildProviders() {
  const select = $("#providerSelect");
  select.textContent = "";
  Object.entries(state.meta.providers).forEach(([name, cfg]) => {
    select.append(h("option", { value: name, text: `${name}（默认 ${cfg.default_model}）`,
      ...(name === state.meta.defaults.provider ? { selected: "" } : {}) }));
  });
  refreshModelList();
  select.addEventListener("change", refreshModelList);
}

function refreshModelList() {
  const cfg = state.meta.providers[$("#providerSelect").value] || { models: [] };
  const list = $("#modelList");
  list.textContent = "";
  cfg.models.forEach((m) => list.append(h("option", { value: m })));
}

async function loadSnapshots() {
  const select = $("#snapshotSelect");
  select.textContent = "";
  try {
    const { snapshots } = await api("/api/snapshots?limit=40");
    snapshots.forEach((s) => select.append(h("option", {
      value: s.snapshot_id,
      text: `${s.snapshot_id.slice(0, 14)} · ${new Date(s.as_of).toLocaleString("zh-CN", { hour12: false })} · ${s.symbols.join("/")} · ${s.evidence} 卡`,
    })));
    if (!snapshots.length) select.append(h("option", { value: "", text: "（账本中暂无 Snapshot）" }));
  } catch (e) {
    select.append(h("option", { value: "", text: `读取失败：${e.message}` }));
  }
}

function bindUI() {
  $("#btnAddSymbol").addEventListener("click", () => {
    const raw = $("#customSymbol").value.trim().toUpperCase();
    if (!raw) return;
    state.pool.add(raw);
    if (!state.meta.pool.includes(raw)) state.meta.pool.push(raw);
    $("#customSymbol").value = "";
    buildPool();
  });
  $("#customSymbol").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#btnAddSymbol").click(); });
  $("#btnRefreshSnapshots").addEventListener("click", loadSnapshots);

  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    state.tab = tab.dataset.tab;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    document.querySelectorAll(".tab-pane").forEach((p) => p.classList.toggle("active", p.id === `tab-${state.tab}`));
    if (state.tab === "replay" && !$("#replayBody").textContent) loadTraces();
    renderActiveTab();
  }));

  $("#searchBox").addEventListener("input", (e) => { state.search = e.target.value.toLowerCase(); renderActiveTab(); });
  $("#btnRun").addEventListener("click", startRun);
  $("#btnCancel").addEventListener("click", cancelRun);
  $("#btnClear").addEventListener("click", resetView);
  $("#btnTraces").addEventListener("click", loadTraces);
  $("#traceSelect").addEventListener("change", () => replayTrace($("#traceSelect").value));
  $("#drawerClose").addEventListener("click", () => $("#drawer").classList.add("hidden"));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#drawer").classList.add("hidden"); });
}

/* ---------------- 运行控制 ---------------- */

function currentOptions() {
  const mode = (document.querySelector('input[name="source"]:checked') || {}).value || "collect";
  let account;
  try { account = JSON.parse($("#accountState").value || "null"); }
  catch (e) { throw new Error(`账户状态 JSON 不合法：${e.message}`); }
  return {
    pool: [...state.pool],
    mode,
    provider: $("#providerSelect").value,
    model: $("#modelInput").value.trim() || null,
    as_of: $("#asOf").value.trim() || null,
    days: Number($("#days").value) || 7,
    market_lookback: Number($("#marketLookback").value) || 220,
    filings: $("#filings").checked,
    run_research: $("#runResearch").checked,
    run_committee: $("#runCommittee").checked,
    snapshot_id: mode === "snapshot" ? $("#snapshotSelect").value : null,
    account_state: account,
  };
}

async function startRun() {
  $("#runError").textContent = "";
  resetView(true);
  try {
    const options = currentOptions();
    const res = await fetch("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(options),
    });
    const body = await res.json();
    if (!res.ok) {
      if (body.run_id) { attach(body.run_id, "已有运行在跑，改为跟随它"); return; }
      throw new Error(body.error || `HTTP ${res.status}`);
    }
    attach(body.run_id, null);
  } catch (e) {
    $("#runError").textContent = e.message;
  }
}

function attach(runId, note) {
  state.runId = runId;
  state.running = true;
  setStatus("running", note || "运行中");
  $("#runId").textContent = runId;
  $("#btnCancel").disabled = false;
  const es = new EventSource(`/api/stream?run_id=${runId}&after=0`);
  state.es = es;
  es.onmessage = (msg) => {
    let event;
    try { event = JSON.parse(msg.data); } catch { return; }
    if (event.type === "stream_end") { es.close(); state.running = false; $("#btnCancel").disabled = true; return; }
    ingest(event);
  };
  es.onerror = () => {
    // 运行结束后服务端按设计关闭连接；若仍在跑则等待浏览器自动重连
    if (!state.running) es.close();
  };
}

async function cancelRun() {
  await api("/api/run/cancel", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: state.runId }) });
  setStatus("running", "已请求停止：等当前阶段/标的边界收尾");
}

function resetView(keepConfig) {
  if (state.es) { state.es.close(); state.es = null; }
  state.events = [];
  state.stages = {};
  state.calls.clear();
  state.order = [];
  state.objects.clear();
  state.expanded.clear();
  state.stageFilter = null;
  state.runId = keepConfig ? state.runId : null;
  state.running = false;
  if (!keepConfig) { setStatus("idle", "待命"); $("#runId").textContent = ""; $("#btnCancel").disabled = true; }
  renderAll();
}

function setStatus(cls, text) {
  $("#statusDot").className = `dot ${cls}`;
  $("#statusText").textContent = text;
}

/* ---------------- 事件索引 ---------------- */

let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; renderAll(); });
}

function ingest(event) {
  state.events.push(event);
  switch (event.type) {
    case "stage_start": state.stages[event.stage] = { status: "running", label: event.label, meta: event, started: event.ts }; break;
    case "stage_done": state.stages[event.stage] = { status: "done", label: event.label, summary: event.summary, duration_ms: event.duration_ms }; break;
    case "stage_skip": state.stages[event.stage] = { status: "skipped", label: event.label, reason: event.reason }; break;
    case "run_meta": state.runId = event.run_id; state.options = event.options || null; $("#runId").textContent = event.run_id; break;
    case "run_end":
      state.running = false;
      $("#btnCancel").disabled = true;
      setStatus(event.state, event.state === "done" ? "运行完成" : `运行${event.state === "cancelled" ? "已停止" : "失败"}`);
      if (state.es) { state.es.close(); state.es = null; }
      break;
    case "node_input": touchCall(event).input = event; break;
    case "tool_call": touchCall(event).tools.push(event); break;
    case "node_output": {
      const call = touchCall(event);
      call.output = event;
      indexCallObjects(call);
      break;
    }
    case "object":
      if (event.id && event.kind !== "raw_record") state.objects.set(event.id, event);
      else state.objects.set(`${event.kind}:${state.events.length}`, event);
      break;
    default: break;
  }
  scheduleRender();
}

function touchCall(event) {
  const key = event.call_id || `${event.node}:${event.symbol}:${state.events.length}`;
  if (!state.calls.has(key)) {
    state.calls.set(key, { key, node: event.node || "unknown", symbol: event.symbol || "", tools: [] });
    state.order.push(key);
  }
  return state.calls.get(key);
}

// 输出里的 claim / verdict 与事件流中的 packet 一并建索引，让对象表不依赖 object 事件顺序
function indexCallObjects(call) {
  const output = (call.output || {}).output;
  if (!output || typeof output !== "object") return;
  (output.claims || []).forEach((claim) => {
    if (!claim.claim_id) return;
    state.objects.set(claim.claim_id, { kind: "claim_card", id: claim.claim_id, data: claim,
      symbol: call.symbol, agent: call.node, pending: true });
  });
}

/* ---------------- 渲染 ---------------- */

function renderAll() { renderPipeline(); renderCounter(); renderActiveTab(); }

function renderCounter() {
  $("#counter").textContent =
    `事件 ${state.events.length} · Agent 调用 ${state.calls.size} · 对象 ${state.objects.size}`;
}

function renderPipeline() {
  const box = $("#pipeline");
  box.textContent = "";
  (state.meta ? state.meta.stages : []).forEach((stage) => {
    const info = state.stages[stage.value] || { status: "pending", label: stage.label };
    const card = h("div", {
      cls: `stage ${info.status}${state.stageFilter === stage.value ? " sel" : ""}`,
      on: { click: () => { state.stageFilter = state.stageFilter === stage.value ? null : stage.value;
        state.tab = "stream"; document.querySelector('.tab[data-tab="stream"]').click(); renderAll(); } },
    }, [
      h("div", { cls: "name", text: info.label || stage.label }),
      h("div", { cls: "body" }, summarizeStage(info)),
      h("div", { cls: "foot" }, [
        badge(info.status, { pending: "待命", running: "进行中", done: "完成", skipped: "跳过", failed: "失败" }[info.status]),
        info.duration_ms != null ? h("span", { cls: "muted tiny mono", text: `${(info.duration_ms / 1000).toFixed(1)}s` }) : null,
      ]),
    ]);
    box.append(card);
  });
}

function summarizeStage(info) {
  const lines = [];
  if (info.status === "skipped") lines.push(h("div", { cls: "muted", text: info.reason || "跳过" }));
  if (info.status === "running") lines.push(h("div", { text: "执行中…" }));
  const s = info.summary;
  if (s) {
    if (s.records != null) {
      const counts = Object.entries(s.counts || {}).map(([k, v]) => k + ":" + v).join(" ");
      lines.push(h("div", { text: "记录 " + s.records + " 条 · " + clip(counts, 90) }));
    }
    if (s.cards != null) {
      lines.push(h("div", { text: "卡片 " + s.cards + "（新建 " + s.created + " / 复用 " + s.reused + "）· " + clip(s.by_kind, 60) }));
    }
    if (s.snapshot_id) lines.push(h("div", {}, [h("span", { text: "快照 " }), idChip("snapshot", s.snapshot_id)]));
    if (s.packets != null) lines.push(h("div", { text: "Packet " + s.packets + " · Claim " + s.claims }));
    if (s.items) {
      lines.push(h("div", { text: "意图 " + s.items.map((i) => i.symbol + ":" + i.action).join(" ") }));
    }
    if (s.order_plan_id) {
      const expo = s.exposure != null ? " · 暴露 " + (s.exposure * 100).toFixed(1) + "%" : "";
      lines.push(h("div", {}, [h("span", { text: "订单 " + s.orders + " · 延后 " + s.deferred + " · 风控调整 " + s.adjustments + expo }), idChip("order_plan", s.order_plan_id)]));
    }
  }
  return lines;
}

function matchesSearch(text) {
  return !state.search || String(text).toLowerCase().includes(state.search);
}

/* ---------- 事件流 ---------- */

function renderStream() {
  const pane = $("#tab-stream");
  pane.textContent = "";
  const events = state.events.filter((e) => (!state.stageFilter || e.stage === state.stageFilter || e.type === "stage_start" || e.type === "stage_done") &&
    matchesSearch(e.text || e.tool || e.kind || ""));
  if (!events.length) { pane.append(h("div", { cls: "empty", text: "暂无事件：左侧配置后点「运行」" })); return; }
  const recent = events.slice(-400);
  if (recent.length < events.length) {
    pane.append(h("div", { cls: "muted tiny", text: `仅渲染最近 400 条（共 ${events.length} 条），可用过滤框缩小范围` }));
  }
  recent.forEach((event) => pane.append(streamRow(event)));
}

function streamRow(event) {
  const row = h("div", { cls: `event ${event.type}${event.level ? ` ${event.level}` : ""}` });
  const head = h("div", { cls: "event-head" });
  head.append(h("span", { cls: "t", text: fmtTime(event.ts) }));
  const label = { stage_start: "阶段开始", stage_done: "阶段完成", stage_skip: "阶段跳过", node_input: "节点输入",
    node_output: "节点输出", tool_call: "工具调用", object: "对象", log: "日志", run_meta: "运行元数据",
    run_end: "运行结束" }[event.type] || event.type;
  head.append(badge(event.node || "", event.node ? `${event.node}` : label));
  if (event.symbol) head.append(h("span", { cls: "mono tiny muted", text: event.symbol }));
  head.append(h("span", { cls: "txt", text: eventText(event) }));
  row.append(head);
  const body = eventBody(event);
  if (body) row.append(h("div", { cls: "event-body" }, body));
  return row;
}

function eventText(event) {
  switch (event.type) {
    case "node_input": {
      const model = event.model ? " · " + event.model : "";
      return "输入注入：" + (event.evidence_count || 0) + " 条证据" + model;
    }
    case "node_output": return event.error ? "失败：" + event.error
      : "输出：" + clip(summarizeOutput(event.output), 120);
    case "tool_call": return event.tool + "(" + clip(event.arguments, 70) + ") → " + event.result_count + " 条";
    case "object": {
      const extra = event.kind === "evidence_card" ? " · " + event.data.kind + "/" + event.data.symbol : "";
      return (KIND_LABEL[event.kind] || event.kind) + " " + shortId(event.id) + extra;
    }
    case "stage_done": return event.label + " 完成 · " + clip(event.summary, 110);
    case "stage_skip": return event.label + " 跳过 · " + event.reason;
    case "run_end": return "运行" + ({ done: "完成", cancelled: "已停止", failed: "失败" }[event.state] || event.state) + " · " + event.duration_s + "s";
    default: return event.text || "";
  }
}

function summarizeOutput(output) {
  if (!output || typeof output !== "object") return String(output || "");
  if (output.route === "data_request") return "data_request：" + clip(output.detail, 80);
  if (output.claims) {
    const stances = output.claims.map((c) => c.stance + "/" + num(c.confidence)).join(" ");
    return output.claims.length + " 张 Claim：" + stances;
  }
  if (output.verdict) return "verdict=" + output.verdict + " · " + (output.reasons ? output.reasons.length : 0) + " 条理由";
  if (output.items) {
    return output.items.length + " 项意图：" + output.items.map((i) => i.symbol + "=" + i.action).join(" ");
  }
  return Object.keys(output).join(", ");
}

function eventBody(event) {
  const out = [];
  if (event.type === "node_input" && event.prompt) {
    out.push(jsonBlock(event.prompt, "注入输入（正文按 320 字截断）"));
  } else if (event.type === "node_output") {
    if (event.output != null) out.push(jsonBlock(event.output, "模型输出"));
    if (event.error) out.push(h("div", { cls: "error", text: event.error }));
  } else if (event.type === "tool_call") {
    if (event.evidence_ids && event.evidence_ids.length) {
      out.push(h("div", { cls: "link-list" }, event.evidence_ids.slice(0, 12).map((id) => idChip("evidence_card", id))));
    }
    if (event.note) out.push(h("div", { cls: "muted tiny", text: event.note }));
    if (event.error) out.push(h("div", { cls: "error tiny", text: JSON.stringify(event.error) }));
    if (event.result_raw) out.push(h("div", { cls: "muted tiny", text: event.result_raw }));
    out.push(jsonBlock({ arguments: event.arguments }, "工具参数"));
  } else if (event.type === "object" || event.type === "stage_done" || event.type === "run_meta") {
    out.push(jsonBlock(event.data || event.options || event.summary || event, "详情"));
  }
  return out.filter(Boolean);
}

/* ---------- Agent 节点 ---------- */

function renderNodes() {
  const pane = $("#tab-nodes");
  pane.textContent = "";
  const keys = state.order.filter((k) => {
    const call = state.calls.get(k);
    return (!state.stageFilter || (state.stageFilter === "committee") === (call.node === "committee")) &&
      matchesSearch(`${call.node} ${call.symbol} ${clip(call.input && call.input.prompt, 9999)}`);
  });
  const bySymbol = groupCalls(keys);
  if (!bySymbol.size && !state.options) {
    pane.append(h("div", { cls: "empty", text: "还没有 Agent 调用记录" }));
    return;
  }
  bySymbol.forEach((calls, symbol) => {
    const block = h("div", { cls: "symbol-block" }, [h("h3", { text: symbol || "（无标的）" })]);
    nodesForSymbol(symbol, calls).forEach((item) =>
      block.append(typeof item === "string" ? silentCard(item, symbol) : nodeCard(item)));
    packetsFor(symbol).forEach((p) => block.append(packetLine(p)));
    pane.append(block);
  });
}

function groupCalls(keys) {
  const bySymbol = new Map();
  keys.forEach((k) => {
    const call = state.calls.get(k);
    const list = bySymbol.get(call.symbol) || [];
    list.push(call);
    bySymbol.set(call.symbol, list);
  });
  bySymbol.forEach((calls) => calls.sort((a, b) => NODE_ORDER.indexOf(a.node) - NODE_ORDER.indexOf(b.node)));
  // 本轮标的池里尚未产生任何调用的标的先占位，让「阶段流转到哪了」一眼可见
  const pool = state.options && state.options.pool;
  if (pool && state.stages.research) pool.forEach((s) => { if (!bySymbol.has(s)) bySymbol.set(s, []); });
  return bySymbol;
}

// 期望节点与实际调用对齐：缺的节点只有在「流水线已越过它」时才判为静默弃权，否则只是还没轮到
function nodesForSymbol(symbol, calls) {
  const present = new Set(calls.map((c) => c.node));
  const wanted = GRAPH_NODES.filter((n) => !present.has(n));
  if (!wanted.length) return calls;
  const past = present.has("risk_critic") ||
    packetsFor(symbol).length > 0 ||
    (state.stages.research || {}).status === "done" ||
    !!((state.stages.committee || {}).status);
  const silent = past ? wanted : [];
  const merged = calls.concat(silent);
  return merged.sort((a, b) => {
    const an = typeof a === "string" ? a : a.node;
    const bn = typeof b === "string" ? b : b.node;
    return NODE_ORDER.indexOf(an) - NODE_ORDER.indexOf(bn);
  });
}

function silentCard(node, symbol) {
  return h("div", { cls: "node-card silent" }, [
    h("div", { cls: "node-head" }, [
      badge(node, node),
      h("span", { cls: "title muted", text: SILENT_HINTS[node] || "无模型调用" }),
      h("span", { cls: "spacer" }),
      badge("skipped", "未触发"),
    ]),
  ]);
}

function packetsFor(symbol) {
  return [...state.objects.values()].filter((o) => o.kind === "research_packet" && (!symbol || o.symbol === symbol));
}

function packetLine(object) {
  const p = object.data;
  return h("div", { cls: "node-card" }, [
    h("div", { cls: "node-head" }, [
      badge("packet", "packet"),
      h("span", { cls: "title", text: "程序收口 ResearchPacket" }),
      idChip("research_packet", object.id, object.id),
      h("span", { cls: "spacer" }),
      h("span", { cls: "muted tiny", text: `引用覆盖 ${(p.citation_coverage * 100).toFixed(0)}%` }),
      badge(p.critic.verdict === "allow" ? "done" : p.critic.verdict === "abstain" ? "warn" : "failed", `critic=${p.critic.verdict}`),
    ]),
  ]);
}

function nodeCard(call) {
  const card = h("div", { cls: "node-card" });
  const head = h("div", {
    cls: "node-head",
    on: { click: () => { state.expanded.has(call.key) ? state.expanded.delete(call.key) : state.expanded.add(call.key); renderNodes(); } },
  }, [
    badge(call.node, call.node),
    h("span", { cls: "title", text: call.input ? clip(call.input.model, 40) : "等待输入" }),
    h("span", { cls: "muted tiny", text: `证据 ${call.input ? call.input.evidence_count || 0 : 0}` }),
    h("span", { cls: "muted tiny", text: `工具 ${call.tools.length}` }),
    h("span", { cls: "spacer" }),
    call.output ? (call.output.error ? badge("failed", "失败") : badge("done", `${(call.output.duration_ms / 1000).toFixed(1)}s`))
      : badge("running", "进行中"),
  ]);
  card.append(head);
  if (!state.expanded.has(call.key)) return card;

  if (call.input) {
    const evidence = (call.input.prompt || {}).evidence || [];
    const section = h("div", { cls: "node-section" }, [h("h4", { text: `注入输入 · ${evidence.length} 条证据` })]);
    const meta = call.input.prompt || {};
    ["agent", "instruction", "symbol", "as_of", "prompt_version"].forEach((k) => {
      if (meta[k] != null) section.append(h("div", { cls: "claim-line" }, [
        h("span", { cls: "st", text: `${k}: ` }), h("span", { text: clip(meta[k], 160) })]));
    });
    if (call.input.upstream_claim_ids && call.input.upstream_claim_ids.length) {
      section.append(h("div", { cls: "claim-line" }, [h("span", { cls: "st", text: "上游论点: " }),
        ...call.input.upstream_claim_ids.map((id) => idChip("claim_card", id))]));
    }
    if (evidence.length) {
      section.append(h("details", {}, [h("summary", { text: "证据清单（点开逐张引用）" }),
        h("div", { cls: "link-list" }, evidence.map((e) => h("div", {}, [
          idChip("evidence_card", e.evidence_id, shortId(e.evidence_id)),
          h("span", { cls: "muted", text: ` ${e.source || ""} · ${clip(e.title, 70)}` })]))) ]));
    }
    section.append(jsonBlock(meta, "完整注入 JSON"));
    card.append(section);
  }

  const toolSection = h("div", { cls: "node-section" }, [h("h4", { text: `工具调用 · ${call.tools.length} 次` })]);
  if (!call.tools.length) toolSection.append(h("div", { cls: "muted tiny", text: "本节点未发起补查（证据由注入 hook 直接提供）" }));
  call.tools.forEach((tool) => toolSection.append(h("div", { cls: "tool-line" }, [
    h("span", { cls: "nm", text: tool.tool }),
    h("span", { text: clip(tool.arguments, 80) }),
    h("span", { cls: "muted", text: `→ ${tool.result_count} 条` }),
    ...(tool.evidence_ids || []).slice(0, 6).map((id) => idChip("evidence_card", id)),
    tool.note ? h("span", { cls: "muted tiny", text: tool.note }) : null,
  ])));
  card.append(toolSection);

  const outSection = h("div", { cls: "node-section" }, [h("h4", { text: `模型输出${call.output && call.output.output ? " · " + clip(summarizeOutput(call.output.output), 60) : ""}` })]);
  if (call.output && call.output.error) outSection.append(h("div", { cls: "error", text: call.output.error }));
  const output = call.output && call.output.output;
  if (output) {
    outSection.append(outputView(output));
    outSection.append(jsonBlock(output, "原始输出 JSON"));
  } else if (!call.output) outSection.append(h("div", { cls: "muted tiny", text: "等待模型返回" }));
  card.append(outSection);
  return card;
}

function outputView(output) {
  const box = h("div");
  if (output.route === "data_request") {
    box.append(h("div", { cls: "claim-line" }, [badge("warn", "data_request"), h("span", { text: ` ${clip(output.detail, 160)}` })]));
    return box;
  }
  (output.claims || []).forEach((c) => box.append(h("div", { cls: "claim-line" }, [
    c.claim_id ? idChip("claim_card", c.claim_id, shortId(c.claim_id)) : badge("", "未落账"),
    badge(c.stance === "bullish" ? "done" : c.stance === "bearish" ? "failed" : "", c.stance),
    h("span", { cls: "muted tiny", text: ` conf ${num(c.confidence)} · 引用 ${(c.supporting_evidence_ids || []).length}` }),
    h("div", { text: clip(c.statement, 200) }),
    ...(c.supporting_evidence_ids || []).slice(0, 6).map((id) => idChip("evidence_card", id)),
  ])));
  if (output.verdict) {
    box.append(h("div", { cls: "claim-line" }, [
      badge(output.verdict === "allow" ? "done" : output.verdict === "abstain" ? "warn" : "failed", `verdict=${output.verdict}`),
      h("ul", {}, (output.reasons || []).map((r) => h("li", { text: clip(r, 180) }))),
    ]));
  }
  (output.items || []).forEach((i) => box.append(h("div", { cls: "claim-line" }, [
    badge(i.action === "abstain" ? "warn" : "done", `${i.symbol} ${i.action}`),
    h("span", { cls: "muted tiny", text: `强度 ${i.strength} · 优先级 ${i.priority}` }),
    h("div", { text: clip(i.rationale, 200) }),
    ...(i.supporting_card_ids || []).map((id) => idChip("claim_card", id)),
  ])));
  return box;
}

/* ---------- 产出对象 ---------- */

function renderObjects() {
  const pane = $("#tab-objects");
  pane.textContent = "";
  const items = [...state.objects.values()];
  if (!items.length) { pane.append(h("div", { cls: "empty", text: "还没有对象产出" })); return; }
  const groups = new Map();
  items.forEach((o) => {
    const kind = o.kind || "unknown";
    groups.set(kind, [...(groups.get(kind) || []), o]);
  });
  ["snapshot", "evidence_card", "claim_card", "research_packet", "thesis_book", "portfolio_intent",
    "intent_constraints", "target_portfolio", "risk_projected_portfolio", "order_plan", "raw_record"]
    .filter((kind) => groups.has(kind))
    .forEach((kind) => pane.append(objectGroup(kind, groups.get(kind))));
  [...groups.keys()].filter((kind) => !KIND_LABEL[kind])
    .forEach((kind) => pane.append(objectGroup(kind, groups.get(kind))));
}

function objectGroup(kind, objects) {
  const filtered = objects.filter((o) => matchesSearch(JSON.stringify(o.data)));
  const table = h("table", { cls: "obj" });
  const head = h("tr");
  const columns = objectColumns(kind);
  columns.forEach((c) => head.append(h("th", { text: c })));
  const body = h("tbody");
  filtered.slice(0, 300).forEach((o) => {
    const tr = h("tr");
    objectCells(kind, o).forEach((cell) => tr.append(h("td", {}, Array.isArray(cell) ? cell : [cell])));
    body.append(tr);
  });
  table.append(h("thead", {}, [head]), body);
  return h("div", { cls: "obj-group" }, [
    h("h3", { text: `${KIND_LABEL[kind] || kind} · ${filtered.length}` }),
    table,
    filtered.length > 300 ? h("div", { cls: "muted tiny", text: `仅列出前 300 条，其余按 ID 在账本回放查看` }) : null,
  ]);
}

function objectColumns(kind) {
  return {
    snapshot: ["Snapshot", "as_of", "标的", "卡数", "trace"],
    evidence_card: ["Evidence", "kind", "标的", "available_at", "来源", "标题/正文摘要"],
    claim_card: ["Claim", "agent", "立场", "置信", "引用"],
    research_packet: ["Packet", "标的", "覆盖率", "critic", "claims", "trace"],
    thesis_book: ["ThesisBook", "packets", "regime", "trace"],
    portfolio_intent: ["Intent", "items", "trace"],
    intent_constraints: ["Constraints", "权重区间/动作", "trace"],
    target_portfolio: ["Target", "暴露", "现金", "weights", "trace"],
    risk_projected_portfolio: ["Projected", "暴露", "现金", "调整", "trace"],
    order_plan: ["OrderPlan", "orders", "延后", "trace"],
    raw_record: ["记录", "kind", "标的", "内容"],
  }[kind] || ["ID", "详情"];
}

function objectCells(kind, object) {
  const d = object.data || {};
  switch (kind) {
    case "snapshot": return [idChip("snapshot", object.id, shortId(object.id)), fmtTime(d.as_of), (d.symbols || []).join("/"),
      (d.evidence_ids || []).length, [idChip("trace", d.trace_id, shortId(d.trace_id))]];
    case "evidence_card": return [idChip("evidence_card", object.id, shortId(object.id)), d.kind, d.symbol,
      fmtTime(d.available_at), clip(d.source, 24),
      h("span", { cls: "clip", text: clip(d.title || d.summary || d.canonical_url || d.content_hash || "", 90) })];
    case "claim_card": return [idChip("claim_card", object.id, shortId(object.id)), d.agent || object.agent || "",
      d.stance, num(d.confidence),
      (d.supporting_evidence_ids || []).slice(0, 4).map((id) => idChip("evidence_card", id))];
    case "research_packet": return [idChip("research_packet", object.id, shortId(object.id)), d.symbol,
      ((d.citation_coverage || 0) * 100).toFixed(0) + "%", (d.critic || {}).verdict,
      (d.claim_card_ids || []).length, [idChip("trace", d.trace_id, shortId(d.trace_id))]];
    case "thesis_book": return [idChip("thesis_book", object.id, shortId(object.id)), (d.packet_ids || []).length,
      d.regime || "—", [idChip("trace", d.trace_id, shortId(d.trace_id))]];
    case "portfolio_intent": return [idChip("portfolio_intent", object.id, shortId(object.id)),
      (d.items || []).map((i) => i.symbol + ":" + i.action).join(" "),
      [idChip("trace", d.trace_id, shortId(d.trace_id))]];
    case "intent_constraints": return [idChip("intent_constraints", object.id, shortId(object.id)),
      h("span", { cls: "clip", text: (d.items || []).map((i) =>
        i.symbol + ":[" + (i.min_weight * 100).toFixed(1) + "," + (i.max_weight * 100).toFixed(1) + "]"
        + (i.force_exit ? "⊗" : i.trading_allowed ? "" : "✋")).join(" ") }),
      [idChip("trace", d.trace_id, shortId(d.trace_id))]];
    case "target_portfolio": return [idChip("target_portfolio", object.id, shortId(object.id)),
      ((d.total_exposure || 0) * 100).toFixed(1) + "%", ((d.cash_weight || 0) * 100).toFixed(1) + "%",
      h("span", { cls: "clip", text: (d.weights || []).map((w) => w.symbol + ":" + (w.weight * 100).toFixed(1) + "%").join(" ") }),
      [idChip("trace", d.trace_id, shortId(d.trace_id))]];
    case "risk_projected_portfolio": return [idChip("risk_projected_portfolio", object.id, shortId(object.id)),
      ((d.total_exposure || 0) * 100).toFixed(1) + "%", ((d.cash_weight || 0) * 100).toFixed(1) + "%",
      h("span", { cls: "clip", text: (d.adjustments || []).map((a) =>
        a.symbol + ":" + (a.from_weight * 100).toFixed(1) + "→" + (a.to_weight * 100).toFixed(1) + "%(" + a.reason_code + ")").join(" ") || "—" }),
      [idChip("trace", d.trace_id, shortId(d.trace_id))]];
    case "order_plan": return [idChip("order_plan", object.id, shortId(object.id)),
      h("span", { cls: "clip", text: (d.orders || []).map((o) =>
        o.side[0].toUpperCase() + " " + o.symbol + " " + (o.target_weight * 100).toFixed(1) + "%" + (o.forced ? "⚡" : "")).join(" ") || "—" }),
      (d.deferred_trades || []).length,
      [idChip("trace", d.trace_id, shortId(d.trace_id))]];
    case "raw_record": return [h("span", { cls: "mono", text: object.id }), d.kind || object.kind || "",
      d.symbol || object.symbol || "",
      h("span", { cls: "clip", text: clip(d.title || d.summary || d.body || d, 120) })];
    default: return [object.id, JSON.stringify(d).slice(0, 120)];
  }
}

/* ---------------- 账本回放 ---------------- */

async function loadTraces() {
  const select = $("#traceSelect");
  select.textContent = "";
  const { traces } = await api("/api/traces?limit=60");
  traces.forEach((t) => select.append(h("option", {
    value: t.trace_id,
    text: `${fmtTime(t.created_at)} · ${t.status} · ${t.trace_id.length > 34 ? t.trace_id.slice(0, 34) + "…" : t.trace_id} · 卡${t.kinds.evidence_card} 论${t.kinds.claim_card} 包${t.kinds.research_packet}`,
  })));
  if (traces.length) replayTrace(traces[0].trace_id);
  else $("#replayBody").append(h("div", { cls: "empty", text: "账本中还没有 trace" }));

  const { runs } = await api("/api/runs?limit=40");
  const box = h("div", { cls: "obj-group" }, [h("h3", { text: `历史运行（本机 JSONL，共 ${runs.length}）` })]);
  if (runs.length) {
    const table = h("table", { cls: "obj" });
    table.append(h("thead", {}, h("tr", {}, ["时间", "状态", "标的", "事件", "耗时", "操作"].map((c) => h("th", { text: c })))));
    const body = h("tbody");
    runs.forEach((r) => body.append(h("tr", {}, [
      h("td", { text: new Date(r.created_at).toLocaleString("zh-CN", { hour12: false }) }),
      h("td", {}, [badge(r.state === "done" ? "done" : r.state === "failed" ? "failed" : "", r.state)]),
      h("td", { text: (r.options && r.options.pool || []).join("/") }),
      h("td", { text: r.events }), h("td", { text: r.duration_s || "" }),
      h("td", {}, h("button", { cls: "ghost tiny", text: "载入事件", on: { click: () => replayRun(r.run_id) } })),
    ])));
    table.append(body);
    box.append(table);
  }
  $("#replayBody").prepend(box);
}

async function replayRun(runId) {
  const detail = await api(`/api/run?run_id=${encodeURIComponent(runId)}`);
  resetView(true);
  state.runId = runId;
  $("#runId").textContent = runId;
  (detail.events || []).forEach(ingest);
  setStatus(detail.state === "done" ? "done" : detail.state === "failed" ? "failed" : "idle", "历史运行重放");
  detail.events && detail.events.length && ingest({ type: "log", level: "muted", text: `已从 ${runId}.jsonl 重放 ${detail.events.length} 条事件` });
  state.tab = "stream";
  document.querySelector('.tab[data-tab="stream"]').click();
}

async function replayTrace(traceId) {
  if (!traceId) return;
  const detail = await api(`/api/trace?trace_id=${encodeURIComponent(traceId)}`);
  const box = $("#replayBody");
  box.querySelectorAll("[data-replay]").forEach((n) => n.remove());
  const wrap = h("div", { "data-replay": "1" });
  wrap.append(h("h3", { text: `trace ${traceId}` }));
  Object.entries(detail.objects || {}).forEach(([kind, list]) => {
    if (!list.length) return;
    wrap.append(objectGroup(kind, list.map((data) => ({ kind, id: data[objectIdKey(kind)], data, trace_id: traceId }))));
  });
  if (detail.tool_calls && detail.tool_calls.length) {
    const table = h("table", { cls: "obj" });
    table.append(h("thead", {}, h("tr", {}, ["节点", "工具", "参数", "命中", "证据"].map((c) => h("th", { text: c })))));
    const body = h("tbody");
    detail.tool_calls.forEach((t) => body.append(h("tr", {}, [
      h("td", { text: t.node }), h("td", { text: t.tool }), h("td", { text: clip(t.arguments, 70) }),
      h("td", { text: t.result_count }),
      h("td", {}, (t.evidence_ids || []).map((id) => idChip("evidence_card", id, shortId(id)))),
    ])));
    table.append(body);
    wrap.append(h("div", { cls: "obj-group" }, [h("h3", { text: `模型补查审计 · ${detail.tool_calls.length}` }), table]));
  }
  if (detail.queries && detail.queries.length) {
    const counts = {};
    detail.queries.forEach((q) => { counts[`${q.symbol}/${q.kind}`] = (counts[`${q.symbol}/${q.kind}`] || 0) + 1; });
    wrap.append(h("div", { cls: "obj-group" }, [h("h3", { text: `Gateway 读取审计 · ${detail.queries.length} 次` }),
      h("div", { cls: "kv tiny" }, Object.entries(counts).map(([k, v]) => h("span", { text: `${k}×${v}` })))]));
  }
  box.append(wrap);
}

const objectIdKey = (kind) => ({ snapshot: "snapshot_id", evidence_card: "evidence_id", claim_card: "claim_id",
  research_packet: "packet_id", thesis_book: "thesis_book_id", portfolio_intent: "intent_id",
  intent_constraints: "intent_id", target_portfolio: "target_portfolio_id",
  risk_projected_portfolio: "projected_portfolio_id", order_plan: "order_plan_id" }[kind] || "object_id");

/* ---------------- 详情抽屉 ---------------- */

const LINK_LABEL = {
  cited_by_claims: "被哪些 Claim 引用",
  contradicted_by_claims: "被哪些 Claim 反驳",
  evidence: "引用证据",
  claims: "包含 Claim",
  packets: "来源 Packet",
  thesis_book_ids: "所属 ThesisBook",
};

// 关联列表的对象类型由链接名确定，不再靠字段启发式猜（猜错会点开查不到的 ID）
const LINK_KIND = {
  cited_by_claims: "claim_card",
  contradicted_by_claims: "claim_card",
  claims: "claim_card",
  evidence: "evidence_card",
  packets: "research_packet",
  thesis_book_ids: "thesis_book",
};

async function openDrawer(kind, id) {
  if (kind === "trace") {
    state.tab = "replay";
    document.querySelector('.tab[data-tab="replay"]').click();
    const select = $("#traceSelect");
    if (![...select.options].some((o) => o.value === id)) {
      select.append(h("option", { value: id, text: id }));
    }
    select.value = id;
    replayTrace(id);
    return;
  }
  const drawer = $("#drawer");
  drawer.classList.remove("hidden");
  $("#drawerKind").textContent = KIND_LABEL[kind] || kind;
  $("#drawerTitle").textContent = id;
  const body = $("#drawerBody");
  body.textContent = "";
  body.append(h("div", { cls: "muted", text: "读取中…" }));
  try {
    if (kind === "raw_record") {
      const local = [...state.objects.values()].find((o) => o.id === id);
      body.textContent = "";
      body.append(jsonBlock(local, "采集记录（未入账本，冻结建卡后可查正文库）"));
      return;
    }
    const detail = await api(`/api/object?kind=${kind}&id=${encodeURIComponent(id)}`);
    body.textContent = "";
    body.append(summarySection(detail));
    const links = detail.links || {};
    Object.entries(links).forEach(([name, list]) => {
      if (!list || !list.length) return;
      body.append(h("section", {}, [h("h4", { text: LINK_LABEL[name] || name }), linkList(name, list)]));
    });
    if (kind === "evidence_card") {
      const raw = await api(`/api/raw?evidence_id=${encodeURIComponent(id)}`);
      body.append(h("section", {}, [h("h4", { text: "原始正文（内容寻址）" }), rawView(raw.payload)]));
    }
    body.append(h("section", {}, [h("h4", { text: "契约载荷" }), jsonBlock(detail.data)]));
    body.append(h("section", {}, [h("h4", { text: "溯源 trace" }), idChip("trace", detail.trace_id, detail.trace_id)]));
  } catch (e) {
    body.textContent = "";
    body.append(h("div", { cls: "error", text: `读取失败：${e.message}` }));
    const local = state.objects.get(id);
    if (local) body.append(jsonBlock(local.data, "事件流里的快照数据"));
  }
}

function summarySection(detail) {
  const d = detail.data || {};
  const rows = [];
  const put = (k, v) => { if (v != null && v !== "") rows.push([k, typeof v === "object" ? JSON.stringify(v) : String(v)]); };
  put("标的", d.symbol);
  put("kind", d.kind);
  put("agent", d.agent);
  put("立场/动作", d.stance || d.action);
  put("置信/强度", d.confidence != null ? num(d.confidence) : d.strength);
  put("发布时间", d.published_at);
  put("可得时间", d.available_at);
  put("as_of", d.as_of);
  put("来源", d.source);
  put("内容哈希", d.content_hash);
  put("URL", d.canonical_url);
  put("论点", d.statement);
  put("理由", d.rationale || (d.reasons || []).join(" | "));
  put("覆盖率", d.citation_coverage != null ? `${(d.citation_coverage * 100).toFixed(0)}%` : null);
  put("critic", d.critic && d.critic.verdict);
  put("创建时间", detail.created_at);
  return h("section", {}, [h("h4", { text: "概要" }),
    h("div", { cls: "kv" }, rows.flatMap(([k, v]) => [h("b", { text: k }), h("span", { text: v })]))]);
}

function linkList(name, list) {
  const kind = LINK_KIND[name] || "claim_card";
  const box = h("div", { cls: "link-list" });
  list.slice(0, 40).forEach((item) => {
    if (typeof item === "string") { box.append(idChip(kind, item)); return; }
    if (item.missing) { box.append(h("div", { cls: "error tiny", text: item.source || "引用缺失" })); return; }
    const id = item[objectIdKey(kind)] || item.object_id;
    const note = item.statement || item.title || item.summary || item.source || "";
    box.append(h("div", {}, [idChip(kind, id),
      h("span", { cls: "muted", text: ` ${[item.symbol, clip(note, 110)].filter(Boolean).join(" · ")}` })]));
  });
  if (list.length > 40) box.append(h("div", { cls: "muted tiny", text: `… 其余 ${list.length - 40} 条略` }));
  return box;
}

function rawView(payload) {
  if (!payload) return h("div", { cls: "muted", text: "正文缺失" });
  const box = h("div");
  ["title", "summary", "source", "url", "published_at", "sentiment_score"].forEach((k) => {
    if (payload[k] != null) box.append(h("div", { cls: "claim-line" }, [h("span", { cls: "st", text: `${k}: ` }),
      h("span", { text: clip(payload[k], 220) })]));
  });
  if (payload.body != null) {
    let parsed = payload.body;
    try { parsed = JSON.parse(payload.body); } catch { /* 正文可能本就是纯文本 */ }
    box.append(jsonBlock(parsed, "body（程序计算的事实）"));
  }
  box.append(jsonBlock(payload, "完整载荷"));
  return box;
}

function renderActiveTab() {
  if (state.tab === "stream") renderStream();
  else if (state.tab === "nodes") renderNodes();
  else if (state.tab === "objects") renderObjects();
}

init().catch((e) => {
  document.body.prepend(h("div", { cls: "error", text: `初始化失败：${e.message}` }));
});
