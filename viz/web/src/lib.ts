/** 展示用的纯函数与映射表，以及裸 ID 摘要的批量补全。 */

import { useEffect, useSyncExternalStore } from "react";
import { api } from "./api";
import type { Digest } from "./types";

// ---------- 阶段归属映射 ----------

/** 节点事件本身不带 stage，阶段过滤靠这张表把调用归回所属阶段。 */
export const NODE_STAGE: Record<string, string> = {
  event: "research",
  fundamental: "research",
  market: "research",
  risk_critic: "research",
  committee: "committee",
};

export const KIND_STAGE: Record<string, string> = {
  snapshot: "freeze",
  evidence_card: "ingest",
  claim_card: "research",
  research_packet: "research",
  thesis_book: "committee",
  portfolio_intent: "committee",
  intent_constraints: "portfolio",
  target_portfolio: "portfolio",
  risk_projected_portfolio: "portfolio",
  order_plan: "portfolio",
  fill: "execute",
  position_episode: "execute",
  outcome_card: "outcome",
  review_label: "review",
};

/** 阶段摘要里的 ID 字段→对象类型：这些字段渲染成可点 chip，而不是裸字符串。 */
export const ID_FIELD_KIND: Record<string, string> = {
  snapshot_id: "snapshot",
  intent_id: "portfolio_intent",
  order_plan_id: "order_plan",
  thesis_book_id: "thesis_book",
  packet_ids: "research_packet",
  trace_id: "trace",
};

export const KIND_LABEL: Record<string, string> = {
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

/** 账本对象的主键名：抽屉关联列表与回放页按它从 payload 里取 ID。 */
export const OBJECT_ID_KEY: Record<string, string> = {
  snapshot: "snapshot_id",
  evidence_card: "evidence_id",
  claim_card: "claim_id",
  research_packet: "packet_id",
  thesis_book: "thesis_book_id",
  portfolio_intent: "intent_id",
  intent_constraints: "intent_id",
  target_portfolio: "target_portfolio_id",
  risk_projected_portfolio: "projected_portfolio_id",
  order_plan: "order_plan_id",
  fill: "fill_id",
  position_episode: "episode_id",
  outcome_card: "outcome_card_id",
  review_label: "review_label_id",
};

export const LINK_LABEL: Record<string, string> = {
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

// 关联列表的对象类型由链接名确定，不靠字段启发式猜（猜错会点开查不到的 ID）
export const LINK_KIND: Record<string, string> = {
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

// ---------- 格式化 ----------

export const shortId = (id?: string | null): string =>
  id ? String(id).replace(/^(ev|cl|rp|tb|pi|snap|viz|fill|ep|outcome|review)_/, "").slice(0, 10) : "—";

export const clip = (value: unknown, n = 90): string => {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  if (!text) return "";
  return text.length > n ? text.slice(0, n) + "…" : text;
};

export const fmtTime = (ts?: string | null): string =>
  ts ? new Date(ts).toLocaleTimeString("zh-CN", { hour12: false }) : "";

export const num = (value: unknown): string => {
  if (typeof value !== "number") return value == null ? "—" : String(value);
  return Number.isInteger(value) ? String(value) : value.toFixed(3);
};

/** 收益/回撤统一按百分比展示：OutcomeCard 的 net_return/active_return/max_drawdown 都是小数。 */
export const pct = (value: unknown): string =>
  typeof value === "number" ? (value * 100).toFixed(2) + "%" : "—";

export const matchesSearch = (text: unknown, search: string): boolean =>
  !search || String(text).toLowerCase().includes(search);

// ---------- 裸 ID 摘要：攒 150ms 后批量问一次 /api/digest ----------

const cache = new Map<string, Digest | null>();
const pending = new Map<string, string>(); // id → kind
const listeners = new Set<() => void>();
let timer: number | null = null;

function notify(): void {
  listeners.forEach((fn) => fn());
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

function queue(kind: string, id: string): void {
  if (cache.has(id) || pending.has(id)) return;
  pending.set(id, kind);
  if (timer == null) timer = window.setTimeout(flush, 150);
}

async function flush(): Promise<void> {
  timer = null;
  if (!pending.size) return;
  // 只删本轮已送出的，超出的留给下一拍，避免一次请求塞进上千个 ID
  const batch = [...pending.entries()].slice(0, 200);
  batch.forEach(([id]) => pending.delete(id));
  const refs = batch.map(([id, kind]) => `${kind}:${id}`);
  try {
    const { digests } = await api<{ digests: Record<string, Digest> }>(
      `/api/digest?ids=${encodeURIComponent(refs.join(","))}`,
    );
    // 账本里查不到也要记成 null，否则每次渲染都会重新排队同一个 ID
    batch.forEach(([id]) => {
      if (!(id in (digests ?? {}))) cache.set(id, null);
    });
    Object.entries(digests ?? {}).forEach(([id, d]) => cache.set(id, d));
    notify();
  } catch {
    /* 摘要只是锦上添花：失败就保持裸短 ID，不打断观测 */
  }
  if (pending.size && timer == null) timer = window.setTimeout(flush, 150);
}

/** 摘要到达后就地补一行字；id 为空时不发请求（调用方可能只是没拿到 ID）。 */
export function useDigest(kind: string, id: string | null): Digest | null {
  useEffect(() => {
    if (id) queue(kind, id);
  }, [kind, id]);
  return useSyncExternalStore(subscribe, () => (id ? cache.get(id) ?? null : null));
}

/** 一句话摘要：标的 + 定性文本；长文本全量展示不截断。 */
export const digestLine = (d: Digest | null | undefined): string =>
  d ? `${d.symbol ? d.symbol + " " : ""}${d.label || ""}`.trim() : "";
