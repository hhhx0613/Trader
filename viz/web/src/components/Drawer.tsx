import { useEffect, useState } from "react";
import { api } from "../api";
import { KIND_LABEL, LINK_KIND, LINK_LABEL, OBJECT_ID_KEY, clip, num } from "../lib";
import type { ObjDetail, VizObject } from "../types";
import { IdChip, JsonBlock } from "./ui";

interface Props {
  target: { kind: string; id: string } | null;
  objects: Map<string, VizObject>;
  onClose: () => void;
}

/** 详情抽屉：契约载荷 + 概要 + 关联列表 + 溯源 trace；证据卡额外按内容寻址取正文。 */
export default function Drawer({ target, objects, onClose }: Props) {
  const [detail, setDetail] = useState<ObjDetail | null>(null);
  const [raw, setRaw] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!target || target.kind === "raw_record") return;
    let cancelled = false;
    setLoading(true);
    setDetail(null);
    setRaw(null);
    setError(null);
    (async () => {
      try {
        const loaded = await api<ObjDetail>(
          `/api/object?kind=${target.kind}&id=${encodeURIComponent(target.id)}`,
        );
        if (cancelled) return;
        setDetail(loaded);
        if (target.kind === "evidence_card") {
          const payload = await api<{ payload: any }>(
            `/api/raw?evidence_id=${encodeURIComponent(target.id)}`,
          );
          if (!cancelled) setRaw(payload.payload ?? null);
        }
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [target]);

  if (!target) return null;

  return (
    <div className="drawer">
      <div className="drawer-head">
        <div>
          <span className="badge">{KIND_LABEL[target.kind] ?? target.kind}</span>
          <h3>{target.id}</h3>
        </div>
        <button className="ghost" onClick={onClose}>
          ✕
        </button>
      </div>
      <div className="drawer-body">
        {target.kind === "raw_record" ? (
          <JsonBlock
            value={[...objects.values()].find((object) => object.id === target.id)}
            label="采集记录（未入账本，冻结建卡后可查正文库）"
          />
        ) : error ? (
          <>
            <div className="error">{`读取失败：${error}`}</div>
            {objects.has(target.id) ? (
              <JsonBlock value={objects.get(target.id)?.data} label="事件流里的快照数据" />
            ) : null}
          </>
        ) : loading ? (
          <div className="muted">读取中…</div>
        ) : detail ? (
          <>
            <SummarySection detail={detail} />
            {Object.entries(detail.links ?? {}).map(([name, list]) =>
              list?.length ? (
                <section key={name}>
                  <h4>{LINK_LABEL[name] ?? name}</h4>
                  <LinkList name={name} list={list} />
                </section>
              ) : null,
            )}
            {target.kind === "evidence_card" ? (
              <section>
                <h4>原始正文（内容寻址）</h4>
                <RawView payload={raw} />
              </section>
            ) : null}
            <section>
              <h4>契约载荷</h4>
              <JsonBlock value={detail.data} />
            </section>
            <section>
              <h4>溯源 trace</h4>
              {detail.trace_id ? <IdChip kind="trace" id={detail.trace_id} /> : <span className="muted">—</span>}
            </section>
          </>
        ) : null}
      </div>
    </div>
  );
}

function SummarySection({ detail }: { detail: ObjDetail }) {
  const d = detail.data ?? {};
  const rows: [string, string][] = [];
  const put = (key: string, value: unknown) => {
    if (value == null || value === "") return;
    rows.push([key, typeof value === "object" ? JSON.stringify(value) : String(value)]);
  };
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
  put("理由", d.rationale || (d.reasons ?? []).join(" | "));
  put("覆盖率", d.citation_coverage != null ? `${(d.citation_coverage * 100).toFixed(0)}%` : null);
  put("critic", d.critic?.verdict);
  put("创建时间", detail.created_at);
  return (
    <section>
      <h4>概要</h4>
      <div className="kv">
        {rows.map(([key, value]) => (
          <FragmentKV key={key} label={key} value={value} />
        ))}
      </div>
    </section>
  );
}

function FragmentKV({ label, value }: { label: string; value: string }) {
  return (
    <>
      <b>{label}</b>
      <span>{value}</span>
    </>
  );
}

function LinkList({ name, list }: { name: string; list: any[] }) {
  const kind = LINK_KIND[name] ?? "claim_card";
  return (
    <div className="link-list">
      {list.slice(0, 40).map((item, index) => {
        if (typeof item === "string") return <IdChip key={`${item}-${index}`} kind={kind} id={item} />;
        if (item?.missing) {
          return (
            <div className="error tiny" key={index}>
              {item.source || "引用缺失"}
            </div>
          );
        }
        const id = item?.[OBJECT_ID_KEY[kind] ?? "object_id"] ?? item?.object_id;
        const note = item?.statement || item?.title || item?.summary || item?.source || "";
        return (
          <div key={`${id}-${index}`}>
            <IdChip kind={kind} id={String(id ?? "")} />
            <span className="muted"> {[item?.symbol, clip(note, 110)].filter(Boolean).join(" · ")}</span>
          </div>
        );
      })}
      {list.length > 40 ? <div className="muted tiny">{`… 其余 ${list.length - 40} 条略`}</div> : null}
    </div>
  );
}

function RawView({ payload }: { payload: any }) {
  if (!payload) return <div className="muted">正文缺失</div>;
  let body = payload.body;
  try {
    body = typeof body === "string" ? JSON.parse(body) : body;
  } catch {
    /* 正文可能本就是纯文本 */
  }
  return (
    <>
      {["title", "summary", "source", "url", "published_at", "sentiment_score"].map((key) =>
        payload[key] == null ? null : (
          <div className="claim-line" key={key}>
            <span className="st">{`${key}: `}</span>
            <span>{clip(payload[key], 220)}</span>
          </div>
        ),
      )}
      {body != null ? <JsonBlock value={body} label="body（程序计算的事实）" /> : null}
      <JsonBlock value={payload} label="完整载荷" />
    </>
  );
}
