/* 研究链路可视化调试台前端：无构建、无框架，只走本机 /api。
 *
 * 数据来源两条：
 *   1) 运行中的事件流（SSE，/api/stream）——阶段流转、每个 Agent 节点的输入/工具/输出、对象产出
 *   2) 账本只读查询（/api/object、/api/trace、/api/raw）——点任意 ID 溯源到持久化事实与正文
 * 页面刷新后事件流不在内存时，可从「账本回放 → 历史运行」按 JSONL 重放。
 */

// 阶段卡片点击过滤：节点调用与产出对象事件本身不带 stage，由前端按归属映射回阶段
const NODE_STAGE = {
  event: "research", fundamental: "research", market: "research", risk_critic: "research",
  committee: "committee",
};
const KIND_STAGE = {
  snapshot: "freeze", evidence_card: "ingest", claim_card: "research", research_packet: "research",
  thesis_book: "committee", portfolio_intent: "committee", intent_constraints: "portfolio",
  target_portfolio: "portfolio", risk_projected_portfolio: "portfolio", order_plan: "portfolio",
  fill: "execute", position_episode: "execute", outcome_card: "outcome", review_label: "review",
};
// 阶段摘要里的 ID 字段→对象类型：这些字段渲染成可点 chip（带 digest 摘要），而不是裸 JSON 字符串
const ID_FIELD_KIND = {
  snapshot_id: "snapshot", intent_id: "portfolio_intent", order_plan_id: "order_plan",
  thesis_book_id: "thesis_book", packet_ids: "research_packet", trace_id: "trace",
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
  fill: "Fill",
  position_episode: "PositionEpisode",
  outcome_card: "OutcomeCard",
  review_label: "ReviewLabel",
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
  objects: new Map(),
  tab: "stream",
  stageFilter: null,
  search: "",
  expanded: new Set(),
  es: null,
  digest: new Map(),        // id → 摘要（null 表示已查过但账本里没有，避免重复请求）
  pendingDigest: new Map(),  // id → kind，本轮渲染新出现的裸 ID，攒起来批量解析
  digestTimer: null,
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

const shortId = (id) => (id ? String(id).replace(/^(ev|cl|rp|tb|pi|snap|viz|fill|ep|outcome|review)_/, "").slice(0, 10) : "—");
const clip = (value, n = 90) => {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  if (!text) return "";
  return text.length > n ? text.slice(0, n) + "…" : text;
};
const fmtTime = (ts) => (ts ? new Date(ts).toLocaleTimeString("zh-CN", { hour12: false }) : "");
const num = (value) => (typeof value === "number" ? (Number.isInteger(value) ? value : value.toFixed(3)) : value);
// 收益/回撤统一按百分比展示：OutcomeCard 的 net_return/active_return/max_drawdown 都是小数
const pct = (value) => (typeof value === "number" ? (value * 100).toFixed(2) + "%" : "—");

// 所有 JSON 区块一律默认折叠（用户要求）：open 形参保留但不再生效，避免逐处删参
function jsonBlock(obj, label = "完整 JSON") {
  return h("details", {}, [h("summary", { text: label }),
    h("pre", { cls: "json", text: JSON.stringify(obj, null, 2) })]);
}

function idChip(kind, id, label) {
  if (!id) return h("span", { cls: "muted", text: "—" });
  const chip = h("span", { cls: "id-chip", title: id, on: { click: () => openDrawer(kind, id) } });
  if (label) { chip.append(h("span", { text: label })); return chip; }
  chip.dataset.id = id;  // 标记裸 ID chip，摘要到达后由 applyDigests 就地补上，不重渲染整页
  chip.append(h("span", { text: shortId(id) }));
  if (state.digest.has(id)) {
    const note = digestLine(state.digest.get(id));
    if (note) chip.append(h("span", { cls: "id-note", text: ` ${note}` }));
  } else {
    queueDigest(kind, id);  // 裸 ID 先占位，摘要到达后就地补上
  }
  return chip;
}

// 摘要行：标的 + 定性文本；意图理由等长文本全量展示不截断，缺内容时返回空串、chip 保持纯短 ID
function digestLine(d) {
  if (!d) return "";
  return `${d.symbol ? d.symbol + " " : ""}${d.label || ""}`.trim();
}

function queueDigest(kind, id) {
  if (state.digest.has(id) || state.pendingDigest.has(id)) return;
  state.pendingDigest.set(id, kind);
  if (!state.digestTimer) state.digestTimer = setTimeout(flushDigests, 150);
}

async function flushDigests() {
  state.digestTimer = null;
  if (!state.pendingDigest.size) return;
  const entries = [...state.pendingDigest.entries()].slice(0, 200);
  entries.forEach(([id]) => state.pendingDigest.delete(id));  // 只删本轮已送的，多余的留给下一拍
  const refs = entries.map(([id, kind]) => `${kind}:${id}`);
  try {
    const { digests } = await api(`/api/digest?ids=${encodeURIComponent(refs.join(","))}`);
    entries.forEach(([id]) => { if (!(id in (digests || {}))) state.digest.set(id, null); });
    Object.entries(digests || {}).forEach(([id, d]) => state.digest.set(id, d));
    applyDigests();  // 摘要到达后只给 chip 就地补一行字，不触发任何 tab 重渲染
  } catch (e) { /* 解析失败保持裸 ID，不打断观测 */ }
  if (state.pendingDigest.size && !state.digestTimer) state.digestTimer = setTimeout(flushDigests, 150);
}

function applyDigests() {
  document.querySelectorAll(".id-chip[data-id]").forEach((chip) => {
    if (chip.querySelector(".id-note")) return;
    const note = digestLine(state.digest.get(chip.dataset.id));
    if (note) chip.append(h("span", { cls: "id-note", text: ` ${note}` }));
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
        on: { change: () => syncSourcePanels(mode.value) } }),
      h("span", { text: mode.label }),
    ]));
  });
  syncSourcePanels(state.meta.run_modes[0].value);
}

// snapshot 与 full/review 节奏各有专属参数：选中时才展开对应面板，避免无关输入混进载荷
function syncSourcePanels(mode) {
  $("#snapshotPick").classList.toggle("hidden", mode !== "snapshot");
  $("#replayPick").classList.toggle("hidden", !(mode === "full" || mode === "review"));
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
    start: $("#replayStart").value.trim() || null,
    end: $("#replayEnd").value.trim() || null,
    force: $("#forceReplay").checked,
    order_plan_id: $("#orderPlanId").value.trim() || null,
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
  state.objects.clear();
  state.expanded.clear();
  state.stageFilter = null;
  streamView.built = false;  // 视图重建：否则增量分支会对着已清空的列表只追加新事件
  dirty.pipeline = true;
  dirty.objects = true;
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

// 脏区标记：每条事件只标记受影响区域，避免一条消息就把阶段卡片/对象表全部重建
const dirty = { pipeline: true, objects: true };

function ingest(event) {
  state.events.push(event);
  switch (event.type) {
    case "stage_start": state.stages[event.stage] = { status: "running", label: event.label, meta: event, started: event.ts }; dirty.pipeline = true; break;
    case "stage_done": state.stages[event.stage] = { status: "done", label: event.label, summary: event.summary, duration_ms: event.duration_ms }; dirty.pipeline = true; break;
    case "stage_skip": state.stages[event.stage] = { status: "skipped", label: event.label, reason: event.reason }; dirty.pipeline = true; break;
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
      dirty.objects = true;
      break;
    }
    case "object":
      if (event.id && event.kind !== "raw_record") state.objects.set(event.id, event);
      else state.objects.set(`${event.kind}:${state.events.length}`, event);
      dirty.objects = true;
      break;
    default: break;
  }
  scheduleRender();
}

function touchCall(event) {
  const key = event.call_id || `${event.node}:${event.symbol}:${state.events.length}`;
  if (!state.calls.has(key)) {
    state.calls.set(key, { key, node: event.node || "unknown", symbol: event.symbol || "", tools: [] });
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

// 事件驱动的渲染入口：只重建脏区；事件流自身走增量追加（见 renderStream），
// 所以一条普通消息不会引起整页闪烁或滚动位置丢失
function renderAll() {
  if (dirty.pipeline) { renderPipeline(); dirty.pipeline = false; }
  renderCounter();
  if (state.tab === "stream") renderStream();
  else if (state.tab === "objects" && dirty.objects) { renderObjects(); dirty.objects = false; }
}

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
  if (s && s.via_replay) {
    // 闭环回放摘要：与联网节奏的 summary 形状不同，单独渲染计数 + ID chip，避免复用上面的分支出现 undefined
    if (s.note) lines.push(h("div", { cls: "muted", text: s.note }));
    const parts = [];
    const push = (label, v) => { if (v != null && v !== "") parts.push(label + " " + v); };
    if (s.market_bars) push("行情bar", Object.values(s.market_bars).reduce((a, b) => a + b, 0));
    push("新闻天", s.news_days); push("卡片", s.cards); push("Packet", s.packets);
    push("订单", s.orders); push("成交", s.fills); push("持仓集", s.episodes);
    push("结算", s.outcomes); push("复盘", s.reviews);
    if (parts.length) lines.push(h("div", { text: parts.join(" · ") }));
    if (s.snapshot_id) lines.push(h("div", {}, [h("span", { text: "快照 " }), idChip("snapshot", s.snapshot_id)]));
    if (s.intent_id) lines.push(h("div", {}, [h("span", { text: "意图 " }), idChip("portfolio_intent", s.intent_id)]));
    if (s.order_plan_id) lines.push(h("div", {}, [h("span", { text: "订单计划 " }), idChip("order_plan", s.order_plan_id)]));
    if (s.actions && s.actions.length) lines.push(h("div", { cls: "muted tiny", text: clip(s.actions.join(" "), 90) }));
    if (s.eligibility && s.eligibility.length) lines.push(h("div", { cls: "muted tiny", text: "记忆资格 " + clip(s.eligibility.join(" "), 90) }));
    return lines;
  }
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

// 事件归属的阶段列表：自带 stage（可能是逗号分隔的多阶段）优先，
// 否则按节点/对象类型映射；未归属事件（通用日志等）在任何过滤下都不显示
function eventStages(event) {
  if (event.stage) return String(event.stage).split(",");
  if (event.type === "node_input" || event.type === "node_output" || event.type === "tool_call") {
    return NODE_STAGE[event.node] ? [NODE_STAGE[event.node]] : [];
  }
  if (event.type === "object") return KIND_STAGE[event.kind] ? [KIND_STAGE[event.kind]] : [];
  return [];
}

function stageFilterLabel(value) {
  const hit = ((state.meta || {}).stages || []).find((s) => s.value === value);
  return hit ? hit.label : value;
}

/* ---------- 事件流 ---------- */

// 增量渲染台账：sig 是过滤条件指纹，同一指纹下匹配集只在尾部增长；
// consumed 记录已渲染的匹配事件数，rows 是 DOM 里现存行数（上限 400）
const streamView = { sig: null, built: false, consumed: 0, rows: 0 };

const streamNoteText = (total) => `仅渲染最近 400 条（共 ${total} 条），可用过滤框缩小范围`;

function renderStream() {
  const pane = $("#tab-stream");
  const sig = `${state.stageFilter}\u0000${state.search}`;
  const matched = state.events.filter((e) => (!state.stageFilter || eventStages(e).includes(state.stageFilter)) &&
    matchesSearch(e.text || e.tool || e.kind || ""));
  if (streamView.built && sig === streamView.sig) {
    // 增量路径：已有行不动，滚动位置与展开态保留；新事件尾部追加，超上限从头修剪
    const fresh = matched.slice(streamView.consumed);
    if (fresh.length) {
      const empty = pane.querySelector(".empty");
      if (empty) empty.remove();
      fresh.forEach((event) => pane.append(streamRow(event)));
      streamView.consumed = matched.length;
      streamView.rows += fresh.length;
      while (streamView.rows > 400) {
        const first = pane.querySelector(".evt");
        if (!first) break;
        first.remove();
        streamView.rows -= 1;
      }
    }
    let note = pane.querySelector(".stream-note");
    if (matched.length > 400) {
      if (!note) { note = h("div", { cls: "muted tiny stream-note" }); pane.prepend(note); }
      note.textContent = streamNoteText(matched.length);
    } else if (note) note.remove();
    return;
  }
  // 首次/过滤条件变化/视图重置：全量重建
  pane.textContent = "";
  streamView.built = true;
  streamView.sig = sig;
  streamView.consumed = matched.length;
  if (!matched.length) {
    streamView.rows = 0;
    const why = state.stageFilter
      ? `阶段「${stageFilterLabel(state.stageFilter)}」暂无匹配事件（再点该卡片可取消过滤）`
      : state.search ? "没有匹配的事件" : "暂无事件：左侧配置后点「运行」";
    pane.append(h("div", { cls: "empty", text: why })); return;
  }
  const recent = matched.slice(-400);
  streamView.rows = recent.length;
  if (recent.length < matched.length) {
    pane.append(h("div", { cls: "muted tiny stream-note", text: streamNoteText(matched.length) }));
  }
  recent.forEach((event) => pane.append(streamRow(event)));
}

function streamRow(event) {
  const key = `ev:${event.seq}`;
  const open = state.expanded.has(key);
  const expandable = hasDetail(event);
  const row = h("div", { cls: `evt ${event.type}${event.level ? ` ${event.level}` : ""}${expandable ? " expandable" : ""}${open ? " open" : ""}` });
  const head = h("div", { cls: "event-head" });
  if (expandable) head.append(h("span", { cls: "caret", text: open ? "▾" : "▸" }));
  head.append(h("span", { cls: "t", text: fmtTime(event.ts) }));
  const label = { stage_start: "阶段开始", stage_done: "阶段完成", stage_skip: "阶段跳过", node_input: "节点输入",
    node_output: "节点输出", tool_call: "工具调用", object: "对象", log: "日志", run_meta: "运行元数据",
    run_end: "运行结束" }[event.type] || event.type;
  head.append(badge(event.node || "", event.node ? `${event.node}` : label));
  if (event.symbol) head.append(h("span", { cls: "mono tiny muted", text: event.symbol }));
  head.append(h("span", { cls: "txt", text: eventText(event) }));
  row.append(head);
  if (!expandable) return row;
  // 整行任意位置点击展开/收起；详情里的可交互元素（chip/折叠区/JSON 文本）不连带收起
  row.addEventListener("click", (e) => {
    if (e.target.closest("summary, .id-chip, a, button, pre")) return;
    toggleRowDetail(row, event, key);
  });
  if (open) row.append(h("div", { cls: "event-body" }, eventBody(event)));
  return row;
}

// 展开/收起只改被点那一行的 DOM，不重渲染事件流（避免点击后整屏闪）
function toggleRowDetail(row, event, key) {
  const open = !state.expanded.has(key);
  if (open) state.expanded.add(key); else state.expanded.delete(key);
  row.classList.toggle("open", open);
  const caret = row.querySelector(".caret");
  if (caret) caret.textContent = open ? "▾" : "▸";
  const body = row.querySelector(":scope > .event-body");
  if (body) body.remove();
  if (open) row.append(h("div", { cls: "event-body" }, eventBody(event)));
}

// 条目是否有可展开详情：原「Agent 节点」tab 的卡片展开能力已并入这里
function hasDetail(event) {
  if (event.type === "node_input") return !!event.prompt;
  if (event.type === "node_output") return event.output != null || !!event.error;
  return ["tool_call", "object", "stage_done", "run_meta"].includes(event.type);
}

function eventText(event) {
  switch (event.type) {
    case "node_input": {
      const model = event.model ? " · " + event.model : "";
      const prompt = event.prompt || {};
      // risk_critic 注入的是上游论点、committee 注入的是研究包，evidence_count 恒为 0，不补读会误导
      const claims = prompt.claims || [];
      const packets = prompt.packets || [];
      const episodes = Object.values(prompt.historical_episodes_by_symbol || {}).flat();
      let unit = "条证据", n = event.evidence_count || 0;
      if (!n && packets.length) { unit = "个研究包"; n = packets.length; }
      else if (!n && claims.length) { unit = "条论点"; n = claims.length; }
      const mem = episodes.length ? ` · 记忆注入 ${episodes.length} 笔` : "";
      return "输入注入：" + (n || 0) + " " + unit + mem + model;
    }
    case "node_output": return event.error ? "失败：" + event.error
      : "输出：" + clip(summarizeOutput(event.output), 120);
    case "tool_call": return event.tool + "(" + clip(event.arguments, 70) + ") → " + event.result_count + " 条";
    case "object": {
      const extra = event.kind === "evidence_card" ? " · " + event.data.kind + "/" + event.data.symbol : "";
      return (KIND_LABEL[event.kind] || event.kind) + " " + shortId(event.id) + extra;
    }
    case "stage_done": return event.label + " 完成 · " + clip(summaryLine(event.summary), 140);
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
  if (event.type === "node_input") {
    const meta = event.prompt || {};
    const evidence = meta.evidence || [];
    const claims = meta.claims || [];
    const packets = meta.packets || [];
    const episodes = Object.entries(meta.historical_episodes_by_symbol || {}).flatMap(([sym, list]) => list.map((e) => [sym, e]));
    const counts = [];
    if (evidence.length || (!claims.length && !packets.length)) counts.push(`${event.evidence_count || evidence.length} 条证据`);
    if (claims.length) counts.push(`${claims.length} 条上游论点`);
    if (packets.length) counts.push(`${packets.length} 个研究包`);
    if (episodes.length) counts.push(`${episodes.length} 笔历史记忆`);
    const section = h("div", { cls: "node-section" }, [
      h("h4", { text: `注入输入 · ${counts.join(" · ") || "无证据/论点批次"}（正文按 320 字截断）` })]);
    ["agent", "instruction", "symbol", "as_of", "prompt_version"].forEach((k) => {
      if (meta[k] != null) section.append(h("div", { cls: "claim-line" }, [
        h("span", { cls: "st", text: `${k}: ` }), h("span", { text: clip(meta[k], 160) })]));
    });
    if ((event.upstream_claim_ids || []).length) {
      section.append(h("div", { cls: "claim-line" }, [h("span", { cls: "st", text: "上游论点: " }),
        ...event.upstream_claim_ids.map((id) => idChip("claim_card", id))]));
    }
    if (episodes.length) {
      // 委员会的记忆注入链路可视化：每笔 episode 直接成 chip，可点开看结算/复盘全文
      section.append(h("div", { cls: "claim-line" }, [h("span", { cls: "st", text: "记忆注入: " }),
        ...episodes.map(([sym, e]) => idChip("position_episode", String(e.episode_id || e)))]));
    }
    if (evidence.length) {
      section.append(h("details", {}, [h("summary", { text: "证据清单（点开逐张引用）" }),
        h("div", { cls: "link-list" }, evidence.map((e) => h("div", {}, [
          idChip("evidence_card", e.evidence_id, shortId(e.evidence_id)),
          h("span", { cls: "muted", text: ` ${e.source || ""} · ${clip(e.title, 70)}` })]))) ]));
    }
    section.append(jsonBlock(meta, "完整注入 JSON", true));
    out.push(section);
  } else if (event.type === "node_output") {
    const section = h("div", { cls: "node-section" }, [
      h("h4", { text: `模型输出${event.output ? " · " + clip(summarizeOutput(event.output), 60) : ""}` })]);
    if (event.error) section.append(h("div", { cls: "error", text: event.error }));
    const output = event.output;
    if (output && typeof output === "object") {
      section.append(outputView(output));
      section.append(jsonBlock(output, "原始输出 JSON", true));
    } else if (output != null) {
      section.append(h("div", { text: String(output) }));
    }
    out.push(section);
  } else if (event.type === "tool_call") {
    const section = h("div", { cls: "node-section" }, [h("h4", { text: "工具调用" })]);
    if (event.evidence_ids && event.evidence_ids.length) {
      section.append(h("div", { cls: "link-list" }, event.evidence_ids.slice(0, 12).map((id) => idChip("evidence_card", id))));
    }
    if (event.note) section.append(h("div", { cls: "muted tiny", text: event.note }));
    if (event.error) section.append(h("div", { cls: "error tiny", text: JSON.stringify(event.error) }));
    if (event.result_raw) section.append(h("div", { cls: "muted tiny", text: event.result_raw }));
    section.append(jsonBlock({ arguments: event.arguments }, "工具参数", true));
    out.push(section);
  } else if (event.type === "stage_done") {
    // 摘要逐字段平铺，ID 字段走 chip 解析出摘要；原始 JSON 只作兜底折叠在下方
    out.push(h("div", { cls: "node-section" }, [h("h4", { text: "阶段产出" }), summaryView(event.summary)]));
    out.push(jsonBlock(event.summary, "原始 JSON"));
  } else if (event.type === "object" || event.type === "run_meta") {
    out.push(jsonBlock(event.data || event.options || event, "详情", true));
  }
  return out.filter(Boolean);
}

// 行头一行摘要：滤掉 via_replay 等内部键，ID 用 shortId，对象/数组折成计数，不再直出 JSON
function summaryLine(s) {
  if (!s || typeof s !== "object") return String(s || "");
  return Object.entries(s)
    .filter(([k, v]) => k !== "via_replay" && v != null && v !== "" && v !== false)
    .map(([k, v]) => {
      if (ID_FIELD_KIND[k]) return Array.isArray(v) ? `${k.replace(/_id$/, "s")}:${v.map(shortId).join(" ")}` : `${k.replace(/_id$/, "")}:${shortId(String(v))}`;
      if (typeof v === "object") return `${k}:${Array.isArray(v) ? v.length : "…"}`;
      return `${k}=${v}`;
    })
    .join(" · ");
}

function summaryView(summary) {
  const box = h("div");
  if (!summary || typeof summary !== "object") return box;
  Object.entries(summary).forEach(([key, value]) => {
    if (value == null || value === "" || value === false || key === "via_replay") return;
    const line = h("div", { cls: "claim-line" }, [h("span", { cls: "st", text: `${key}: ` })]);
    const kind = ID_FIELD_KIND[key];
    if (kind && Array.isArray(value)) {
      value.forEach((id) => line.append(idChip(kind, id), document.createTextNode(" ")));
    } else if (kind) {
      line.append(idChip(kind, String(value)));
    } else if (key === "items" && Array.isArray(value)) {
      line.append(h("span", { text: value.map((i) => `${i.symbol}:${i.action}(强度${i.strength},P${i.priority})`).join(" ") }));
    } else if (typeof value === "object") {
      line.append(h("span", { text: JSON.stringify(value) }));
    } else {
      line.append(h("span", { text: String(value) }));
    }
    box.append(line);
  });
  return box;
}

/* ---------- 模型输出的结构化视图（事件流 node_output 展开详情用） ---------- */

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
  // 委员会 intent 的每条决策：结构化平铺全部内部字段，理由全文不截断（用户要求）
  (output.items || []).forEach((i) => {
    const line = h("div", { cls: "claim-line" }, [
      badge(i.action === "abstain" ? "warn" : "done", `${i.symbol} ${i.action}`),
      h("span", { cls: "muted tiny", text: ` 持有 ${i.horizon_days != null ? i.horizon_days + "d" : "—"} · 强度 ${i.strength} · 优先级 P${i.priority}` }),
    ]);
    if (i.rationale) line.append(h("div", { text: i.rationale }));
    if (i.no_trade_reason) line.append(h("div", { cls: "muted tiny", text: "不交易原因：" + i.no_trade_reason }));
    if ((i.supporting_card_ids || []).length) line.append(h("div", { cls: "claim-line" }, [
      h("span", { cls: "st", text: "引用卡: " }), ...i.supporting_card_ids.map((id) => idChip("claim_card", id))]));
    if ((i.opposing_card_ids || []).length) line.append(h("div", { cls: "claim-line" }, [
      h("span", { cls: "st", text: "反方卡: " }), ...i.opposing_card_ids.map((id) => idChip("claim_card", id))]));
    if ((i.calibration_episode_ids || []).length) line.append(h("div", { cls: "claim-line" }, [
      h("span", { cls: "st", text: "校准记忆: " }), ...i.calibration_episode_ids.map((id) => idChip("position_episode", id))]));
    box.append(line);
  });
  return box;
}

/* ---------- 产出对象 ---------- */

function renderObjects() {
  const pane = $("#tab-objects");
  pane.textContent = "";
  const items = [...state.objects.values()].filter((o) => !state.stageFilter || KIND_STAGE[o.kind] === state.stageFilter);
  if (!items.length) {
    pane.append(h("div", { cls: "empty", text: state.stageFilter ? `阶段「${stageFilterLabel(state.stageFilter)}」没有产出对象（再点阶段卡片可取消过滤）` : "还没有对象产出" }));
    return;
  }
  const groups = new Map();
  items.forEach((o) => {
    const kind = o.kind || "unknown";
    groups.set(kind, [...(groups.get(kind) || []), o]);
  });
  ["snapshot", "evidence_card", "claim_card", "research_packet", "thesis_book", "portfolio_intent",
    "intent_constraints", "target_portfolio", "risk_projected_portfolio", "order_plan",
    "fill", "position_episode", "outcome_card", "review_label", "raw_record"]
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
    fill: ["Fill", "标的", "方向", "成交/请求", "成交价", "状态", "trace"],
    position_episode: ["Episode", "标的", "状态", "锚定→到期", "horizon", "rev", "trace"],
    outcome_card: ["Outcome", "标的", "净收益", "超额", "最大回撤", "观察", "trace"],
    review_label: ["Review", "状态", "记忆准入", "理由", "trace"],
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
    case "fill": return [idChip("fill", object.id, shortId(object.id)), d.symbol, d.side,
      `${d.filled_shares}/${d.requested_shares}`,
      d.executed_price != null ? num(d.executed_price) : "—", d.status,
      [idChip("trace", d.trace_id, shortId(d.trace_id))]];
    case "position_episode": return [idChip("position_episode", object.id, shortId(object.id)), d.symbol,
      d.status + (d.is_carry_in ? "·carry-in" : ""),
      fmtTime(d.anchor_at) + "→" + fmtTime(d.current_due_at),
      d.original_horizon_days + "d", "r" + (d.revision || 1),
      [idChip("trace", d.trace_id, shortId(d.trace_id))]];
    case "outcome_card": return [idChip("outcome_card", object.id, shortId(object.id)), d.symbol,
      pct(d.net_return), pct(d.active_return), pct(d.max_drawdown), d.observation,
      [idChip("trace", d.trace_id, shortId(d.trace_id))]];
    case "review_label": return [idChip("review_label", object.id, shortId(object.id)), d.status,
      d.memory_eligibility, h("span", { cls: "clip", text: clip(d.rationale, 120) }),
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
  risk_projected_portfolio: "projected_portfolio_id", order_plan: "order_plan_id",
  fill: "fill_id", position_episode: "episode_id", outcome_card: "outcome_card_id",
  review_label: "review_label_id" }[kind] || "object_id");

/* ---------------- 详情抽屉 ---------------- */

const LINK_LABEL = {
  cited_by_claims: "被哪些 Claim 引用",
  contradicted_by_claims: "被哪些 Claim 反驳",
  evidence: "引用证据",
  claims: "包含 Claim",
  packets: "来源 Packet",
  thesis_book_ids: "所属 ThesisBook",
  fills: "成交 Fill",
  outcome_card: "结果卡 OutcomeCard",
  review_label: "复盘标签 ReviewLabel",
  episode: "持仓案例 Episode",
  calibration_episodes: "校准引用案例（记忆→委员会）",
  intents_citing: "引用该案例的意图",
};

// 关联列表的对象类型由链接名确定，不再靠字段启发式猜（猜错会点开查不到的 ID）
const LINK_KIND = {
  cited_by_claims: "claim_card",
  contradicted_by_claims: "claim_card",
  claims: "claim_card",
  evidence: "evidence_card",
  packets: "research_packet",
  thesis_book_ids: "thesis_book",
  fills: "fill",
  outcome_card: "outcome_card",
  review_label: "review_label",
  episode: "position_episode",
  calibration_episodes: "position_episode",
  intents_citing: "portfolio_intent",
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
  else if (state.tab === "objects") renderObjects();
}

init().catch((e) => {
  document.body.prepend(h("div", { cls: "error", text: `初始化失败：${e.message}` }));
});
