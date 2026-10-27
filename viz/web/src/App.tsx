import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { api, apiUrl, postJson } from "./api";
import ConfigPanel from "./components/ConfigPanel";
import Drawer from "./components/Drawer";
import EventStream from "./components/EventStream";
import ObjectsTab from "./components/ObjectsTab";
import Pipeline from "./components/Pipeline";
import ReplayTab from "./components/ReplayTab";
import { LinkContext } from "./components/ui";
import type { Meta, RunDetail, RunEvent, RunPayload, RunCall, StageInfo, VizObject } from "./types";

type TabKey = "stream" | "objects" | "replay";

interface State {
  meta: Meta | null;
  runId: string | null;
  running: boolean;
  status: { cls: string; text: string };
  events: RunEvent[];
  stages: Record<string, StageInfo>;
  calls: Map<string, RunCall>;
  objects: Map<string, VizObject>;
  expanded: Set<number>;
  tab: TabKey;
  stageFilter: string | null;
  error: string | null;
}

const initial: State = {
  meta: null,
  runId: null,
  running: false,
  status: { cls: "idle", text: "待命" },
  events: [],
  stages: {},
  calls: new Map(),
  objects: new Map(),
  expanded: new Set(),
  tab: "stream",
  stageFilter: null,
  error: null,
};

type Action =
  | { type: "meta"; meta: Meta }
  | { type: "status"; cls: string; text: string }
  | { type: "attach"; runId: string; note?: string }
  | { type: "streamEnd" }
  | { type: "event"; event: RunEvent }
  | { type: "loadRun"; runId: string; events: RunEvent[]; state: string }
  | { type: "tab"; tab: TabKey }
  | { type: "stageFilter"; stage: string | null }
  | { type: "toggleExpand"; seq: number }
  | { type: "runError"; error: string | null }
  | { type: "reset"; keepRun: boolean };

/** 单条事件 → 索引更新。纯函数，历史运行重放也复用它。 */
function ingest(state: State, event: RunEvent): State {
  const events = [...state.events, event];
  switch (event.type) {
    case "stage_start":
      return {
        ...state,
        events,
        stages: {
          ...state.stages,
          [event.stage!]: { status: "running", label: event.label ?? event.stage! },
        },
      };
    case "stage_done":
      return {
        ...state,
        events,
        stages: {
          ...state.stages,
          [event.stage!]: {
            status: "done",
            label: event.label ?? event.stage!,
            summary: event.summary,
            duration_ms: event.duration_ms,
          },
        },
      };
    case "stage_skip":
      return {
        ...state,
        events,
        stages: {
          ...state.stages,
          [event.stage!]: {
            status: "skipped",
            label: event.label ?? event.stage!,
            reason: event.reason,
          },
        },
      };
    case "run_meta":
      return { ...state, events, runId: event.run_id ?? state.runId };
    case "run_end":
      return {
        ...state,
        events,
        running: false,
        status: {
          cls: event.state ?? "idle",
          text:
            event.state === "done"
              ? "运行完成"
              : `运行${event.state === "cancelled" ? "已停止" : "失败"}`,
        },
      };
    case "node_input":
    case "tool_call":
    case "node_output": {
      const key = event.call_id ?? `${event.node}:${event.symbol}:${events.length}`;
      const call: RunCall = state.calls.get(key) ?? {
        key,
        node: event.node ?? "unknown",
        symbol: event.symbol ?? "",
        tools: [],
      };
      const calls = new Map(state.calls);
      let objects = state.objects;
      if (event.type === "node_input") {
        calls.set(key, { ...call, input: event });
      } else if (event.type === "tool_call") {
        calls.set(key, { ...call, tools: [...call.tools, event] });
      } else {
        calls.set(key, { ...call, output: event });
        // 输出里的 claim 与事件流中的 packet 一并建索引，对象表不依赖 object 事件到达顺序
        const output = event.output;
        if (output && typeof output === "object" && Array.isArray(output.claims)) {
          objects = new Map(state.objects);
          for (const claim of output.claims) {
            if (!claim?.claim_id) continue;
            objects.set(claim.claim_id, {
              kind: "claim_card",
              id: claim.claim_id,
              data: claim,
              symbol: call.symbol,
              agent: call.node,
              pending: true,
            });
          }
        }
      }
      return { ...state, events, calls, objects };
    }
    case "object": {
      const objects = new Map(state.objects);
      if (event.id && event.kind !== "raw_record") {
        objects.set(event.id, {
          kind: event.kind ?? "unknown",
          id: event.id,
          data: event.data,
          symbol: event.symbol,
          agent: event.agent,
          trace_id: event.trace_id,
        });
      } else {
        // 采集记录没有稳定身份：按到达顺序占位，只在抽屉里看
        objects.set(`${event.kind}:${events.length}`, {
          kind: event.kind ?? "unknown",
          id: `${event.kind}:${events.length}`,
          data: event.data,
        });
      }
      return { ...state, events, objects };
    }
    default:
      return { ...state, events };
  }
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "meta":
      return { ...state, meta: action.meta };
    case "status":
      return { ...state, status: { cls: action.cls, text: action.text } };
    case "attach":
      return {
        ...state,
        runId: action.runId,
        running: true,
        status: { cls: "running", text: action.note ?? "运行中" },
      };
    case "streamEnd":
      return { ...state, running: false };
    case "event":
      return ingest(state, action.event);
    case "loadRun": {
      const base: State = { ...initial, meta: state.meta, runId: action.runId, tab: "stream" };
      // 重排 seq：JSONL 里的 seq 有跳号（run_meta 不入文件），重排后可直接当 DOM key
      const events = action.events.map((event, index) => ({ ...event, seq: index }));
      const built = events.reduce<State>((acc, event) => ingest(acc, event), base);
      // 补一条日志：历史重放与实时运行在事件流里要能一眼区分
      const replayed = events.length
        ? ingest(built, {
            seq: events.length,
            ts: new Date().toISOString(),
            type: "log",
            level: "muted",
            text: `已从 ${action.runId}.jsonl 重放 ${events.length} 条事件`,
          })
        : built;
      return {
        ...replayed,
        status: {
          cls: action.state === "done" ? "done" : action.state === "failed" ? "failed" : "idle",
          text: "历史运行重放",
        },
      };
    }
    case "tab":
      return { ...state, tab: action.tab };
    case "stageFilter":
      return { ...state, stageFilter: action.stage };
    case "toggleExpand": {
      const expanded = new Set(state.expanded);
      if (expanded.has(action.seq)) expanded.delete(action.seq);
      else expanded.add(action.seq);
      return { ...state, expanded };
    }
    case "runError":
      return { ...state, error: action.error };
    case "reset":
      return {
        ...initial,
        meta: state.meta,
        tab: state.tab,
        runId: action.keepRun ? state.runId : null,
        status: action.keepRun ? state.status : initial.status,
      };
    default:
      return state;
  }
}

export default function App() {
  const [state, dispatch] = useReducer(reducer, initial);
  const [search, setSearch] = useState("");
  const [drawer, setDrawer] = useState<{ kind: string; id: string } | null>(null);
  const [traceFocus, setTraceFocus] = useState<string | null>(null);
  const esRef = useRef<EventSource | null>(null);

  // 一次运行只允许一条 SSE：重复 attach 会先关掉旧的，避免事件流双份
  const attach = useCallback((runId: string, note?: string) => {
    esRef.current?.close();
    dispatch({ type: "attach", runId, note });
    const es = new EventSource(apiUrl(`/api/stream?run_id=${encodeURIComponent(runId)}&after=0`));
    esRef.current = es;
    es.onmessage = (message) => {
      let event: RunEvent;
      try {
        event = JSON.parse(message.data);
      } catch {
        return;
      }
      if (event.type === "stream_end") {
        es.close();
        if (esRef.current === es) esRef.current = null;
        dispatch({ type: "streamEnd" });
        return;
      }
      if (event.type === "run_end") {
        es.close();
        if (esRef.current === es) esRef.current = null;
      }
      dispatch({ type: "event", event });
    };
  }, []);

  useEffect(() => {
    api<Meta>("/api/meta")
      .then((meta) => dispatch({ type: "meta", meta }))
      .catch((e) => dispatch({ type: "runError", error: `初始化失败：${(e as Error).message}` }));
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDrawer(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => () => esRef.current?.close(), []);

  const startRun = useCallback(
    async (payload: RunPayload) => {
      dispatch({ type: "runError", error: null });
      dispatch({ type: "reset", keepRun: true });
      try {
        // 手写 fetch：409 时响应里带 run_id，要据此「跟随已有运行」而不是当失败
        const res = await fetch(apiUrl("/api/run"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const body = await res.json();
        if (!res.ok) {
          if (body.run_id) {
            attach(body.run_id, "已有运行在跑，改为跟随它");
            return;
          }
          throw new Error(body.error || `HTTP ${res.status}`);
        }
        attach(body.run_id);
      } catch (e) {
        dispatch({ type: "runError", error: (e as Error).message });
      }
    },
    [attach],
  );

  async function cancelRun() {
    if (!state.runId) return;
    try {
      await postJson("/api/run/cancel", { run_id: state.runId });
      dispatch({ type: "status", cls: "running", text: "已请求停止：等当前阶段/标的边界收尾" });
    } catch (e) {
      dispatch({ type: "runError", error: (e as Error).message });
    }
  }

  async function replayRun(runId: string) {
    try {
      const detail = await api<RunDetail>(`/api/run?run_id=${encodeURIComponent(runId)}`);
      dispatch({ type: "loadRun", runId, events: detail.events ?? [], state: detail.state });
    } catch (e) {
      dispatch({ type: "runError", error: (e as Error).message });
    }
  }

  const openObject = useCallback((kind: string, id: string) => {
    if (kind === "trace") {
      setTraceFocus(id);
      dispatch({ type: "tab", tab: "replay" });
      return;
    }
    setDrawer({ kind, id });
  }, []);

  const toggleExpand = useCallback((seq: number) => dispatch({ type: "toggleExpand", seq }), []);
  const consumeTraceFocus = useCallback(() => setTraceFocus(null), []);

  const stageLabel = useCallback(
    (stage: string) => state.meta?.stages.find((item) => item.value === stage)?.label ?? stage,
    [state.meta],
  );

  function selectStage(stage: string) {
    dispatch({ type: "stageFilter", stage: state.stageFilter === stage ? null : stage });
    dispatch({ type: "tab", tab: "stream" });
  }

  const { meta, runId, running, status, events, stages, calls, objects, expanded, tab, stageFilter } =
    state;

  return (
    <LinkContext.Provider value={openObject}>
      <header className="topbar">
        <div className="brand">
          <span className="logo">◆</span>
          <div>
            <h1>研究链路可视化调试台</h1>
            <p className="sub">
              采集 → 建卡 → 冻结 → 逐标的研究图 → 委员会 ｜ 只读本机账本，不改动研究逻辑
            </p>
          </div>
        </div>
        <div className="runbar">
          <span className={`dot ${status.cls}`} />
          <span>{status.text}</span>
          <span className="mono muted">{runId ?? ""}</span>
          <button className="ghost" onClick={cancelRun} disabled={!running}>
            停止
          </button>
          <button
            className="ghost"
            onClick={() => {
              dispatch({ type: "reset", keepRun: false });
              dispatch({ type: "runError", error: null });
            }}
          >
            清空视图
          </button>
        </div>
      </header>

      <main className="layout">
        <ConfigPanel meta={meta} running={running} onRun={startRun} />

        <section className="workspace">
          <Pipeline
            stages={meta?.stages ?? []}
            info={stages}
            filter={stageFilter}
            onSelect={selectStage}
          />

          <nav className="tabs">
            {(
              [
                ["stream", "事件流"],
                ["objects", "产出对象"],
                ["replay", "账本回放"],
              ] as [TabKey, string][]
            ).map(([value, label]) => (
              <button
                key={value}
                className={`tab${tab === value ? " active" : ""}`}
                onClick={() => dispatch({ type: "tab", tab: value })}
              >
                {label}
              </button>
            ))}
            <div className="tab-tools">
              <input
                type="search"
                placeholder="过滤事件/对象…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <span className="muted tiny">{`事件 ${events.length} · Agent 调用 ${calls.size} · 对象 ${objects.size}`}</span>
            </div>
          </nav>

          <div className="tab-body">
            {tab === "stream" ? (
              <EventStream
                events={events}
                stageFilter={stageFilter}
                search={search.toLowerCase()}
                expanded={expanded}
                onToggle={toggleExpand}
                stageLabel={stageLabel}
              />
            ) : tab === "objects" ? (
              <ObjectsTab
                objects={objects}
                stageFilter={stageFilter}
                search={search.toLowerCase()}
                stageLabel={stageLabel}
              />
            ) : (
              <ReplayTab
                focusTrace={traceFocus}
                onFocusConsumed={consumeTraceFocus}
                onReplayRun={replayRun}
              />
            )}
          </div>
          {state.error ? <div className="error">{state.error}</div> : null}
        </section>
      </main>

      <Drawer target={drawer} objects={objects} onClose={() => setDrawer(null)} />
    </LinkContext.Provider>
  );
}
