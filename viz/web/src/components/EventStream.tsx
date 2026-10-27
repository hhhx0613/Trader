import { Fragment, memo, useMemo, type MouseEvent } from "react";
import { ID_FIELD_KIND, KIND_LABEL, KIND_STAGE, NODE_STAGE, clip, fmtTime, matchesSearch, num, shortId } from "../lib";
import type { RunEvent } from "../types";
import { Badge, IdChip, JsonBlock } from "./ui";

const MAX_ROWS = 400;

const EVENT_LABEL: Record<string, string> = {
  stage_start: "阶段开始",
  stage_done: "阶段完成",
  stage_skip: "阶段跳过",
  node_input: "节点输入",
  node_output: "节点输出",
  tool_call: "工具调用",
  object: "对象",
  log: "日志",
  run_meta: "运行元数据",
  run_end: "运行结束",
};

interface Props {
  events: RunEvent[];
  stageFilter: string | null;
  search: string;
  expanded: Set<number>;
  onToggle: (seq: number) => void;
  stageLabel: (stage: string) => string;
}

/**
 * 事件流：只渲染最近 400 条。行顶平铺会把同一件事说三遍（回放的一条日志 =
 * 日志行 + 阶段行 + 对象行），所以先过 foldEvents 去重，再按决策日插分隔条。
 */
export default function EventStream({ events, stageFilter, search, expanded, onToggle, stageLabel }: Props) {
  const fold = useMemo(() => foldEvents(events), [events]);

  const matched = useMemo(
    () =>
      events.filter(
        (event) =>
          !fold.hidden.has(event.seq) &&
          !duplicatedByNodes(event, fold.nodeStages) &&
          (!stageFilter || eventStages(event).includes(stageFilter)) &&
          matchesSearch(event.text || event.tool || event.kind || "", search),
      ),
    [events, fold, stageFilter, search],
  );

  if (!matched.length) {
    const why = stageFilter
      ? `阶段「${stageLabel(stageFilter)}」暂无匹配事件（再点该卡片可取消过滤）`
      : search
        ? "没有匹配的事件"
        : "暂无事件：左侧配置后点「运行」";
    return <div className="empty">{why}</div>;
  }

  const recent = matched.length > MAX_ROWS ? matched.slice(-MAX_ROWS) : matched;
  return (
    <>
      {matched.length > MAX_ROWS ? (
        <div className="muted tiny stream-note">{`仅渲染最近 ${MAX_ROWS} 条（共 ${matched.length} 条），可用过滤框缩小范围`}</div>
      ) : null}
      {withDayDividers(recent).map(({ event, divider }) => (
        <Fragment key={event.seq}>
          {divider ? <div className="evt-divider">{divider}</div> : null}
          <EventRow
            event={event}
            tail={fold.tails.get(event.seq)}
            history={fold.history.get(event.seq)}
            open={expanded.has(event.seq)}
            onToggle={onToggle}
          />
        </Fragment>
      ))}
    </>
  );
}

const EventRow = memo(function EventRow({
  event,
  tail,
  history,
  open,
  onToggle,
}: {
  event: RunEvent;
  tail?: string;
  history?: RunEvent[];
  open: boolean;
  onToggle: (seq: number) => void;
}) {
  const expandable = hasDetail(event);
  const handleClick = (e: MouseEvent<HTMLDivElement>) => {
    // 详情里的可交互元素（折叠区/chip/链接/JSON 文本）不连带收起整行
    if ((e.target as HTMLElement).closest("summary, .id-chip, a, button, pre")) return;
    onToggle(event.seq);
  };
  const classes = ["evt", event.type];
  if (event.level) classes.push(event.level);
  if (expandable) classes.push("expandable");
  if (open) classes.push("open");
  return (
    <div className={classes.join(" ")} onClick={expandable ? handleClick : undefined}>
      <div className="event-head">
        {expandable ? <span className="caret">{open ? "▾" : "▸"}</span> : null}
        <span className="t">{fmtTime(event.ts)}</span>
        <Badge kind={event.node || ""}>{event.node || EVENT_LABEL[event.type] || event.type}</Badge>
        {event.symbol ? <span className="mono tiny muted">{event.symbol}</span> : null}
        <span className="txt">
          {eventText(event)}
          {tail ? <span className="muted tiny">{` ${tail}`}</span> : null}
        </span>
      </div>
      {expandable && open ? (
        <div className="event-body">
          <EventBody event={event} history={history} />
        </div>
      ) : null}
    </div>
  );
});

interface Folded {
  hidden: Set<number>;
  tails: Map<number, string>;
  history: Map<number, RunEvent[]>;
  nodeStages: Set<string>;
}

/**
 * 一遍算出「不值得单独占一行」的事件。全部是渲染层判断：事件仍照常进 SSE、照常落 JSONL。
 * - 带 stage 的回放日志行：`_replay_sink` 把同一条日志既发成 log、又拆成 stage_done 摘要，两处都画就是说两遍；
 *   未映射到阶段的回放日志（seed-episode / followup-scheduled / INCOMPLETE）不带 stage，全部保留
 * - 已收口的 stage_start：它只表达「进行中」，顶部阶段卡在说同一件事；未收口前仍然画，长阶段跑着有个进度凭据
 * - 同阶段第二条起的 stage_done（回放每个决策日重发一次）：只留首次，其余折进它的展开体
 * - 研究图/委员会这类只有一个模型节点的阶段：节点行已经把注入与输出讲完，阶段行不重复画
 */
function foldEvents(events: RunEvent[]): Folded {
  const nodeStages = new Set<string>();
  const closed = new Set<string>();
  const doneByStage = new Map<string, RunEvent[]>();
  for (const event of events) {
    const stage = event.node ? NODE_STAGE[event.node] : undefined;
    if (stage && (event.type === "node_input" || event.type === "node_output" || event.type === "tool_call")) {
      nodeStages.add(stage);
    }
    if (!event.stage) continue;
    if (event.type === "stage_done" || event.type === "stage_skip") closed.add(event.stage);
    if (event.type === "stage_done") {
      doneByStage.set(event.stage, [...(doneByStage.get(event.stage) ?? []), event]);
    }
  }
  const hidden = new Set<number>();
  const tails = new Map<number, string>();
  const history = new Map<number, RunEvent[]>();
  for (const event of events) {
    if (event.type === "log" && event.stage) hidden.add(event.seq);
    else if (event.type === "stage_start" && event.stage && closed.has(event.stage)) hidden.add(event.seq);
  }
  for (const rows of doneByStage.values()) {
    if (rows.length < 2) continue;
    const [first] = rows;
    const rest = rows.slice(1);
    for (const row of rest) hidden.add(row.seq);
    history.set(first.seq, rest);
    tails.set(first.seq, `（该阶段共 ${rows.length} 次收口，逐日明细展开看）`);
  }
  return { hidden, tails, history, nodeStages };
}

/** 回放按决策日推进：换日时插一条分隔，长流里才知道自己在哪一天。 */
function withDayDividers(rows: RunEvent[]): { event: RunEvent; divider: string | null }[] {
  let lastDay: string | null = null;
  return rows.map((event) => {
    const day = event.day ?? (typeof event.summary?.day === "string" ? event.summary.day : null);
    const divider = day && day !== lastDay ? day : null;
    if (day) lastDay = day;
    return { event, divider };
  });
}

/** 阶段行只在「这个阶段没有模型节点」时才是唯一记录；有节点的阶段不重复画。 */
function duplicatedByNodes(event: RunEvent, nodeStages: Set<string>): boolean {
  if (event.type !== "stage_start" && event.type !== "stage_done") return false;
  return !!event.stage && nodeStages.has(event.stage);
}

/** 事件归属的阶段列表：自带 stage（可能逗号分隔多阶段）优先，否则按节点/对象类型映射。 */
function eventStages(event: RunEvent): string[] {
  if (event.stage) return String(event.stage).split(",");
  if (event.type === "node_input" || event.type === "node_output" || event.type === "tool_call") {
    return NODE_STAGE[event.node ?? ""] ? [NODE_STAGE[event.node ?? ""]] : [];
  }
  if (event.type === "object") return KIND_STAGE[event.kind ?? ""] ? [KIND_STAGE[event.kind ?? ""]] : [];
  return [];
}

function hasDetail(event: RunEvent): boolean {
  if (event.type === "node_input") return !!event.prompt;
  if (event.type === "node_output") return event.output != null || !!event.error;
  return ["tool_call", "object", "stage_done", "run_meta"].includes(event.type);
}

function eventText(event: RunEvent): string {
  switch (event.type) {
    case "node_input": {
      const model = event.model ? " · " + event.model : "";
      const prompt = event.prompt ?? {};
      // risk_critic 注入上游论点、committee 注入研究包，evidence_count 恒为 0，不补读会误导
      const claims = prompt.claims ?? [];
      const packets = prompt.packets ?? [];
      const episodes = Object.values((prompt.historical_episodes_by_symbol ?? {}) as Record<string, unknown[]>).flat();
      let unit = "条证据";
      let count = event.evidence_count || 0;
      if (!count && packets.length) {
        unit = "个研究包";
        count = packets.length;
      } else if (!count && claims.length) {
        unit = "条论点";
        count = claims.length;
      }
      const memory = episodes.length ? ` · 记忆注入 ${episodes.length} 笔` : "";
      return `输入注入：${count || 0} ${unit}${memory}${model}`;
    }
    case "node_output":
      return event.error ? "失败：" + event.error : "输出：" + clip(summarizeOutput(event.output), 120);
    case "tool_call":
      return `${event.tool}(${clip(event.arguments, 70)}) → ${event.result_count} 条`;
    case "object": {
      const extra = event.kind === "evidence_card" ? ` · ${event.data?.kind}/${event.data?.symbol}` : "";
      return `${KIND_LABEL[event.kind ?? ""] ?? event.kind} ${shortId(event.id)}${extra}`;
    }
    case "stage_done":
      return `${event.label} 完成 · ${clip(summaryLine(event.summary), 140)}`;
    case "stage_skip":
      return `${event.label} 跳过 · ${event.reason}`;
    case "run_end":
      return `运行${{ done: "完成", cancelled: "已停止", failed: "失败" }[event.state ?? ""] ?? event.state} · ${event.duration_s}s`;
    default:
      return event.text ?? "";
  }
}

function summarizeOutput(output: any): string {
  if (!output || typeof output !== "object") return String(output ?? "");
  if (output.route === "data_request") return "data_request：" + clip(output.detail, 80);
  if (output.claims) {
    return `${output.claims.length} 张 Claim：${output.claims
      .map((c: any) => `${c.stance}/${num(c.confidence)}`)
      .join(" ")}`;
  }
  if (output.verdict) return `verdict=${output.verdict} · ${output.reasons ? output.reasons.length : 0} 条理由`;
  if (output.items) {
    return `${output.items.length} 项意图：${output.items.map((i: any) => `${i.symbol}=${i.action}`).join(" ")}`;
  }
  return Object.keys(output).join(", ");
}

/** 行头一行摘要：滤掉内部键，ID 用 shortId，对象/数组折成计数，不直出 JSON。 */
function summaryLine(summary?: Record<string, any>): string {
  if (!summary || typeof summary !== "object") return String(summary ?? "");
  return Object.entries(summary)
    .filter(([key, value]) => key !== "via_replay" && value != null && value !== "" && value !== false)
    .map(([key, value]) => {
      if (ID_FIELD_KIND[key]) {
        return Array.isArray(value)
          ? `${key.replace(/_id$/, "s")}:${value.map(shortId).join(" ")}`
          : `${key.replace(/_id$/, "")}:${shortId(String(value))}`;
      }
      if (typeof value === "object") return `${key}:${Array.isArray(value) ? value.length : "…"}`;
      return `${key}=${value}`;
    })
    .join(" · ");
}

function EventBody({ event, history }: { event: RunEvent; history?: RunEvent[] }) {
  if (event.type === "node_input") {
    const prompt = event.prompt ?? {};
    const evidence: any[] = prompt.evidence ?? [];
    const claims: any[] = prompt.claims ?? [];
    const packets: any[] = prompt.packets ?? [];
    const episodes: any[] = Object.values(
      (prompt.historical_episodes_by_symbol ?? {}) as Record<string, any[]>,
    ).flat();

    const counts: string[] = [];
    if (evidence.length || (!claims.length && !packets.length)) {
      counts.push(`${event.evidence_count || evidence.length} 条证据`);
    }
    if (claims.length) counts.push(`${claims.length} 条上游论点`);
    if (packets.length) counts.push(`${packets.length} 个研究包`);
    if (episodes.length) counts.push(`${episodes.length} 笔历史记忆`);

    return (
      <div className="node-section">
        <h4>{`注入输入 · ${counts.join(" · ") || "无证据/论点批次"}（正文按 320 字截断）`}</h4>
        {["agent", "instruction", "symbol", "as_of", "prompt_version"].map((key) =>
          prompt[key] == null ? null : (
            <div className="claim-line" key={key}>
              <span className="st">{key}: </span>
              <span>{clip(prompt[key], 160)}</span>
            </div>
          ),
        )}
        {event.upstream_claim_ids?.length ? (
          <div className="claim-line">
            <span className="st">上游论点: </span>
            {event.upstream_claim_ids.map((id) => (
              <IdChip key={id} kind="claim_card" id={id} />
            ))}
          </div>
        ) : null}
        {episodes.length ? (
          <div className="claim-line">
            <span className="st">记忆注入: </span>
            {episodes.map((episode, index) => (
              <IdChip
                key={index}
                kind="position_episode"
                id={String(episode?.episode_id ?? episode)}
              />
            ))}
          </div>
        ) : null}
        {evidence.length ? (
          <details>
            <summary>证据清单（点开逐张引用）</summary>
            <div className="link-list">
              {evidence.map((item) => (
                <div key={item.evidence_id}>
                  <IdChip kind="evidence_card" id={item.evidence_id} label={shortId(item.evidence_id)} />
                  <span className="muted">{` ${item.source || ""} · ${clip(item.title, 70)}`}</span>
                </div>
              ))}
            </div>
          </details>
        ) : null}
        <JsonBlock value={prompt} label="完整注入 JSON" />
      </div>
    );
  }

  if (event.type === "node_output") {
    return (
      <div className="node-section">
        <h4>{`模型输出${event.output ? " · " + clip(summarizeOutput(event.output), 60) : ""}`}</h4>
        {event.error ? <div className="error">{event.error}</div> : null}
        {event.output && typeof event.output === "object" ? (
          <>
            <OutputView output={event.output} />
            <JsonBlock value={event.output} label="原始输出 JSON" />
          </>
        ) : event.output != null ? (
          <div>{String(event.output)}</div>
        ) : null}
      </div>
    );
  }

  if (event.type === "tool_call") {
    return (
      <div className="node-section">
        <h4>工具调用</h4>
        {event.evidence_ids?.length ? (
          <div className="link-list">
            {event.evidence_ids.slice(0, 12).map((id) => (
              <IdChip key={id} kind="evidence_card" id={id} />
            ))}
          </div>
        ) : null}
        {event.note ? <div className="muted tiny">{event.note}</div> : null}
        {event.error ? <div className="error tiny">{JSON.stringify(event.error)}</div> : null}
        {event.result_raw ? <div className="muted tiny">{event.result_raw}</div> : null}
        <JsonBlock value={{ arguments: event.arguments }} label="工具参数" />
      </div>
    );
  }

  if (event.type === "stage_done") {
    return (
      <>
        <div className="node-section">
          <h4>阶段产出</h4>
          <SummaryView summary={event.summary} />
        </div>
        {history?.length ? (
          <div className="node-section">
            <h4>{`历次收口 · ${history.length + 1} 次（含本行）`}</h4>
            <ul className="refresh-list">
              {history.map((row) => (
                <li key={row.seq}>{`${row.summary?.day ?? fmtTime(row.ts)} · ${summaryLine(row.summary)}`}</li>
              ))}
            </ul>
          </div>
        ) : null}
        <JsonBlock value={event.summary} label="原始 JSON" />
      </>
    );
  }

  if (event.type === "object") {
    return <JsonBlock value={event.data ?? event} />;
  }
  if (event.type === "run_meta") {
    return <JsonBlock value={event.options ?? event} label="运行参数" />;
  }
  return null;
}

function SummaryView({ summary }: { summary?: Record<string, any> }) {
  if (!summary || typeof summary !== "object") return null;
  return (
    <>
      {Object.entries(summary).map(([key, value]) => {
        if (value == null || value === "" || value === false || key === "via_replay") return null;
        const kind = ID_FIELD_KIND[key];
        return (
          <div className="claim-line" key={key}>
            <span className="st">{`${key}: `}</span>
            {kind && Array.isArray(value) ? (
              value.map((id) => <IdChip key={String(id)} kind={kind} id={String(id)} />)
            ) : kind ? (
              <IdChip kind={kind} id={String(value)} />
            ) : key === "items" && Array.isArray(value) ? (
              <span>
                {value
                  .map((item: any) => `${item.symbol}:${item.action}(强度${item.strength},P${item.priority})`)
                  .join(" ")}
              </span>
            ) : typeof value === "object" ? (
              <span>{JSON.stringify(value)}</span>
            ) : (
              <span>{String(value)}</span>
            )}
          </div>
        );
      })}
    </>
  );
}

/** 模型输出的结构化视图：claim / critic verdict / 委员会意图逐字段平铺，理由全文不截断。 */
function OutputView({ output }: { output: any }) {
  if (output.route === "data_request") {
    return (
      <div className="claim-line">
        <Badge kind="warn">data_request</Badge>
        <span>{` ${output.detail ?? ""}`}</span>
      </div>
    );
  }
  return (
    <>
      {(output.claims ?? []).map((claim: any, index: number) => (
        <div className="claim-line" key={claim.claim_id ?? index}>
          {claim.claim_id ? (
            <IdChip kind="claim_card" id={claim.claim_id} label={shortId(claim.claim_id)} />
          ) : (
            <Badge>未落账</Badge>
          )}
          <Badge kind={claim.stance === "bullish" ? "done" : claim.stance === "bearish" ? "failed" : ""}>
            {claim.stance}
          </Badge>
          <span className="muted tiny">
            {` conf ${num(claim.confidence)} · 引用 ${(claim.supporting_evidence_ids ?? []).length}`}
          </span>
          <div>{claim.statement}</div>
          {(claim.supporting_evidence_ids ?? []).slice(0, 6).map((id: string) => (
            <IdChip key={id} kind="evidence_card" id={id} />
          ))}
        </div>
      ))}
      {output.verdict ? (
        <div className="claim-line">
          <Badge
            kind={output.verdict === "allow" ? "done" : output.verdict === "abstain" ? "warn" : "failed"}
          >
            {`verdict=${output.verdict}`}
          </Badge>
          <ul>
            {(output.reasons ?? []).map((reason: string, index: number) => (
              <li key={index}>{reason}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {(output.items ?? []).map((item: any, index: number) => (
        <div className="claim-line" key={`${item.symbol}-${index}`}>
          <Badge kind={item.action === "abstain" ? "warn" : "done"}>
            {`${item.symbol} ${item.action}`}
          </Badge>
          <span className="muted tiny">
            {` 持有 ${item.horizon_days != null ? item.horizon_days + "d" : "—"} · 强度 ${item.strength} · 优先级 P${item.priority}`}
          </span>
          {item.rationale ? <div>{item.rationale}</div> : null}
          {item.no_trade_reason ? (
            <div className="muted tiny">{"不交易原因：" + item.no_trade_reason}</div>
          ) : null}
          {item.supporting_card_ids?.length ? (
            <div className="claim-line">
              <span className="st">引用卡: </span>
              {item.supporting_card_ids.map((id: string) => (
                <IdChip key={id} kind="claim_card" id={id} />
              ))}
            </div>
          ) : null}
          {item.opposing_card_ids?.length ? (
            <div className="claim-line">
              <span className="st">反方卡: </span>
              {item.opposing_card_ids.map((id: string) => (
                <IdChip key={id} kind="claim_card" id={id} />
              ))}
            </div>
          ) : null}
          {item.calibration_episode_ids?.length ? (
            <div className="claim-line">
              <span className="st">校准记忆: </span>
              {item.calibration_episode_ids.map((id: string) => (
                <IdChip key={id} kind="position_episode" id={id} />
              ))}
            </div>
          ) : null}
        </div>
      ))}
    </>
  );
}
