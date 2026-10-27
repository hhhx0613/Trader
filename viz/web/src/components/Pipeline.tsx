import { clip } from "../lib";
import type { StageInfo } from "../types";
import { Badge, IdChip } from "./ui";

const STATUS_LABEL: Record<string, string> = {
  pending: "待命",
  running: "进行中",
  done: "完成",
  skipped: "跳过",
  failed: "失败",
};

interface Props {
  stages: { value: string; label: string }[];
  info: Record<string, StageInfo>;
  filter: string | null;
  onSelect: (stage: string) => void;
}

/** 顶部阶段卡片：点击即按该阶段过滤事件流（再点一次取消）。 */
export default function Pipeline({ stages, info, filter, onSelect }: Props) {
  return (
    <div className="pipeline">
      {stages.map((stage) => {
        const current: StageInfo = info[stage.value] ?? { status: "pending", label: stage.label };
        return (
          <div
            key={stage.value}
            className={`stage ${current.status}${filter === stage.value ? " sel" : ""}`}
            onClick={() => onSelect(stage.value)}
          >
            <div className="name">{current.label || stage.label}</div>
            <div className="body">
              <StageBody info={current} />
            </div>
            <div className="foot">
              <Badge kind={current.status}>{STATUS_LABEL[current.status] ?? current.status}</Badge>
              {current.duration_ms != null ? (
                <span className="muted tiny mono">{(current.duration_ms / 1000).toFixed(1)}s</span>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** 阶段摘要有两种形状：联网/决策节奏（flow 产出）与闭环回放（run_replay 计数）。 */
function StageBody({ info }: { info: StageInfo }) {
  if (info.status === "skipped") return <div className="muted">{info.reason || "跳过"}</div>;
  if (info.status === "running") return <div>执行中…</div>;
  const summary = info.summary;
  if (!summary) return null;
  return summary.via_replay ? <ReplaySummary summary={summary} /> : <FlowSummary summary={summary} />;
}

function FlowSummary({ summary }: { summary: Record<string, any> }) {
  return (
    <>
      {summary.records != null ? (
        <div>
          {`记录 ${summary.records} 条 · `}
          {clip(
            Object.entries(summary.counts ?? {})
              .map(([k, v]) => `${k}:${v}`)
              .join(" "),
            90,
          )}
        </div>
      ) : null}
      {summary.cards != null ? (
        <div>
          {`卡片 ${summary.cards}（新建 ${summary.created} / 复用 ${summary.reused}）· `}
          {clip(summary.by_kind, 60)}
        </div>
      ) : null}
      {summary.snapshot_id ? (
        <div>
          <span>快照 </span>
          <IdChip kind="snapshot" id={summary.snapshot_id} />
        </div>
      ) : null}
      {summary.packets != null ? (
        <div>{`Packet ${summary.packets} · Claim ${summary.claims}`}</div>
      ) : null}
      {summary.items ? (
        <div>{`意图 ${summary.items.map((i: any) => `${i.symbol}:${i.action}`).join(" ")}`}</div>
      ) : null}
      {summary.order_plan_id ? (
        <div>
          <span>
            {`订单 ${summary.orders} · 延后 ${summary.deferred} · 风控调整 ${summary.adjustments}`}
            {summary.exposure != null ? ` · 暴露 ${(summary.exposure * 100).toFixed(1)}%` : ""}
          </span>
          <IdChip kind="order_plan" id={summary.order_plan_id} />
        </div>
      ) : null}
    </>
  );
}

function ReplaySummary({ summary }: { summary: Record<string, any> }) {
  const parts: string[] = [];
  const push = (label: string, value: unknown) => {
    if (value != null && value !== "") parts.push(`${label} ${value}`);
  };
  if (summary.market_bars) {
    push("行情bar", Object.values(summary.market_bars as Record<string, number>).reduce((a, b) => a + b, 0));
  }
  push("新闻天", summary.news_days);
  push("卡片", summary.cards);
  push("Packet", summary.packets);
  push("订单", summary.orders);
  push("成交", summary.fills);
  push("持仓集", summary.episodes);
  push("结算", summary.outcomes);
  push("复盘", summary.reviews);
  return (
    <>
      {summary.note ? <div className="muted">{summary.note}</div> : null}
      {parts.length ? <div>{parts.join(" · ")}</div> : null}
      {summary.snapshot_id ? (
        <div>
          <span>快照 </span>
          <IdChip kind="snapshot" id={summary.snapshot_id} />
        </div>
      ) : null}
      {summary.intent_id ? (
        <div>
          <span>意图 </span>
          <IdChip kind="portfolio_intent" id={summary.intent_id} />
        </div>
      ) : null}
      {summary.order_plan_id ? (
        <div>
          <span>订单计划 </span>
          <IdChip kind="order_plan" id={summary.order_plan_id} />
        </div>
      ) : null}
      {summary.actions?.length ? (
        <div className="muted tiny">{clip(summary.actions.join(" "), 90)}</div>
      ) : null}
      {summary.eligibility?.length ? (
        <div className="muted tiny">{`记忆资格 ${clip(summary.eligibility.join(" "), 90)}`}</div>
      ) : null}
    </>
  );
}
