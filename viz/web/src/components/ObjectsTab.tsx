import type { ReactNode } from "react";
import { KIND_LABEL, KIND_STAGE, clip, fmtTime, matchesSearch, num, pct, shortId } from "../lib";
import type { VizObject } from "../types";
import { IdChip } from "./ui";

const GROUP_ORDER = [
  "snapshot",
  "evidence_card",
  "claim_card",
  "research_packet",
  "thesis_book",
  "portfolio_intent",
  "intent_constraints",
  "target_portfolio",
  "risk_projected_portfolio",
  "order_plan",
  "fill",
  "position_episode",
  "outcome_card",
  "review_label",
  "raw_record",
];

interface Props {
  objects: Map<string, VizObject>;
  stageFilter: string | null;
  search: string;
  stageLabel: (stage: string) => string;
}

export default function ObjectsTab({ objects, stageFilter, search, stageLabel }: Props) {
  const items = [...objects.values()].filter(
    (object) => !stageFilter || KIND_STAGE[object.kind] === stageFilter,
  );
  if (!items.length) {
    return (
      <div className="empty">
        {stageFilter
          ? `阶段「${stageLabel(stageFilter)}」没有产出对象（再点阶段卡片可取消过滤）`
          : "还没有对象产出"}
      </div>
    );
  }

  const groups = new Map<string, VizObject[]>();
  items.forEach((object) => {
    const kind = object.kind || "unknown";
    groups.set(kind, [...(groups.get(kind) ?? []), object]);
  });

  // 已登记的类别按流水线顺序，未登记的（新对象类型）追加在后面，避免新 kind 直接隐身
  const ordered = [
    ...GROUP_ORDER.filter((kind) => groups.has(kind)),
    ...[...groups.keys()].filter((kind) => !KIND_LABEL[kind]),
  ];

  return (
    <>
      {ordered.map((kind) => (
        <ObjectGroup key={kind} kind={kind} objects={groups.get(kind) ?? []} search={search} />
      ))}
    </>
  );
}

/** 按 kind 分组的对象表；账本回放页也复用它渲染 trace 里的对象。 */
export function ObjectGroup({
  kind,
  objects,
  search = "",
}: {
  kind: string;
  objects: VizObject[];
  search?: string;
}) {
  const filtered = objects.filter((object) => matchesSearch(JSON.stringify(object.data), search));
  const columns = objectColumns(kind);
  return (
    <div className="obj-group">
      <h3>{`${KIND_LABEL[kind] ?? kind} · ${filtered.length}`}</h3>
      <table className="obj">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {filtered.slice(0, 300).map((object, index) => (
            <tr key={`${object.id}-${index}`}>
              {objectCells(kind, object).map((cell, cellIndex) => (
                <td key={cellIndex}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {filtered.length > 300 ? (
        <div className="muted tiny">仅列出前 300 条，其余按 ID 在账本回放查看</div>
      ) : null}
    </div>
  );
}

const COLUMNS: Record<string, string[]> = {
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
};

function objectColumns(kind: string): string[] {
  return COLUMNS[kind] ?? ["ID", "详情"];
}

const traceChip = (traceId?: string) =>
  traceId ? <IdChip kind="trace" id={traceId} label={shortId(traceId)} /> : <span className="muted">—</span>;

function objectCells(kind: string, object: VizObject): ReactNode[] {
  const d = object.data ?? {};
  switch (kind) {
    case "snapshot":
      return [
        <IdChip key="id" kind="snapshot" id={object.id} label={shortId(object.id)} />,
        fmtTime(d.as_of),
        (d.symbols ?? []).join("/"),
        (d.evidence_ids ?? []).length,
        traceChip(d.trace_id),
      ];
    case "evidence_card":
      return [
        <IdChip key="id" kind="evidence_card" id={object.id} label={shortId(object.id)} />,
        d.kind,
        d.symbol,
        fmtTime(d.available_at),
        clip(d.source, 24),
        <span className="clip" key="title">
          {clip(d.title || d.summary || d.canonical_url || d.content_hash || "", 90)}
        </span>,
      ];
    case "claim_card":
      return [
        <IdChip key="id" kind="claim_card" id={object.id} label={shortId(object.id)} />,
        d.agent || object.agent || "",
        d.stance,
        num(d.confidence),
        (d.supporting_evidence_ids ?? []).slice(0, 4).map((id: string) => (
          <IdChip key={id} kind="evidence_card" id={id} />
        )),
      ];
    case "research_packet":
      return [
        <IdChip key="id" kind="research_packet" id={object.id} label={shortId(object.id)} />,
        d.symbol,
        ((d.citation_coverage ?? 0) * 100).toFixed(0) + "%",
        (d.critic ?? {}).verdict,
        (d.claim_card_ids ?? []).length,
        traceChip(d.trace_id),
      ];
    case "thesis_book":
      return [
        <IdChip key="id" kind="thesis_book" id={object.id} label={shortId(object.id)} />,
        (d.packet_ids ?? []).length,
        d.regime || "—",
        traceChip(d.trace_id),
      ];
    case "portfolio_intent":
      return [
        <IdChip key="id" kind="portfolio_intent" id={object.id} label={shortId(object.id)} />,
        (d.items ?? []).map((i: any) => `${i.symbol}:${i.action}`).join(" "),
        traceChip(d.trace_id),
      ];
    case "intent_constraints":
      return [
        <IdChip key="id" kind="intent_constraints" id={object.id} label={shortId(object.id)} />,
        <span className="clip" key="weights">
          {(d.items ?? [])
            .map(
              (i: any) =>
                `${i.symbol}:[${(i.min_weight * 100).toFixed(1)},${(i.max_weight * 100).toFixed(1)}]` +
                (i.force_exit ? "⊗" : i.trading_allowed ? "" : "✋"),
            )
            .join(" ")}
        </span>,
        traceChip(d.trace_id),
      ];
    case "target_portfolio":
      return [
        <IdChip key="id" kind="target_portfolio" id={object.id} label={shortId(object.id)} />,
        ((d.total_exposure ?? 0) * 100).toFixed(1) + "%",
        ((d.cash_weight ?? 0) * 100).toFixed(1) + "%",
        <span className="clip" key="weights">
          {(d.weights ?? []).map((w: any) => `${w.symbol}:${(w.weight * 100).toFixed(1)}%`).join(" ")}
        </span>,
        traceChip(d.trace_id),
      ];
    case "risk_projected_portfolio":
      return [
        <IdChip key="id" kind="risk_projected_portfolio" id={object.id} label={shortId(object.id)} />,
        ((d.total_exposure ?? 0) * 100).toFixed(1) + "%",
        ((d.cash_weight ?? 0) * 100).toFixed(1) + "%",
        <span className="clip" key="adjustments">
          {(d.adjustments ?? [])
            .map(
              (a: any) =>
                `${a.symbol}:${(a.from_weight * 100).toFixed(1)}→${(a.to_weight * 100).toFixed(1)}%(${a.reason_code})`,
            )
            .join(" ") || "—"}
        </span>,
        traceChip(d.trace_id),
      ];
    case "order_plan":
      return [
        <IdChip key="id" kind="order_plan" id={object.id} label={shortId(object.id)} />,
        <span className="clip" key="orders">
          {(d.orders ?? [])
            .map(
              (o: any) =>
                `${o.side[0].toUpperCase()} ${o.symbol} ${(o.target_weight * 100).toFixed(1)}%` +
                (o.forced ? "⚡" : ""),
            )
            .join(" ") || "—"}
        </span>,
        (d.deferred_trades ?? []).length,
        traceChip(d.trace_id),
      ];
    case "fill":
      return [
        <IdChip key="id" kind="fill" id={object.id} label={shortId(object.id)} />,
        d.symbol,
        d.side,
        `${d.filled_shares}/${d.requested_shares}`,
        d.executed_price != null ? num(d.executed_price) : "—",
        d.status,
        traceChip(d.trace_id),
      ];
    case "position_episode":
      return [
        <IdChip key="id" kind="position_episode" id={object.id} label={shortId(object.id)} />,
        d.symbol,
        d.status + (d.is_carry_in ? "·carry-in" : ""),
        fmtTime(d.anchor_at) + "→" + fmtTime(d.current_due_at),
        d.original_horizon_days + "d",
        "r" + (d.revision || 1),
        traceChip(d.trace_id),
      ];
    case "outcome_card":
      return [
        <IdChip key="id" kind="outcome_card" id={object.id} label={shortId(object.id)} />,
        d.symbol,
        pct(d.net_return),
        pct(d.active_return),
        pct(d.max_drawdown),
        d.observation,
        traceChip(d.trace_id),
      ];
    case "review_label":
      return [
        <IdChip key="id" kind="review_label" id={object.id} label={shortId(object.id)} />,
        d.status,
        d.memory_eligibility,
        <span className="clip" key="rationale">
          {clip(d.rationale, 120)}
        </span>,
        traceChip(d.trace_id),
      ];
    case "raw_record":
      return [
        <span className="mono" key="id">
          {object.id}
        </span>,
        d.kind || object.kind || "",
        d.symbol || object.symbol || "",
        <span className="clip" key="body">
          {clip(d.title || d.summary || d.body || d, 120)}
        </span>,
      ];
    default:
      return [object.id, JSON.stringify(d).slice(0, 120)];
  }
}
