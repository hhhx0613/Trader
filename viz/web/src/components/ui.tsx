import { createContext, useContext, type ReactNode } from "react";
import { digestLine, shortId, useDigest } from "../lib";

/** 任意 chip 点开后打开对象：trace 走回放页，其余开抽屉。默认空实现，便于孤立渲染组件。 */
export const LinkContext = createContext<(kind: string, id: string) => void>(() => {});

export function Badge({ kind, children }: { kind?: string; children: ReactNode }) {
  return <span className={`badge ${kind ?? ""}`}>{children}</span>;
}

/** JSON 区块直接铺开：点开节点就是要看全部，不该再点第二下；label 只作分段小标题。 */
export function JsonBlock({ value, label = "完整 JSON" }: { value: unknown; label?: string }) {
  return (
    <div className="json-block">
      <div className="json-label">{label}</div>
      <pre className="json">{JSON.stringify(value, null, 2)}</pre>
    </div>
  );
}

/**
 * 账本对象引用 chip。给了 label 就直接显示文本（不查摘要），
 * 否则先显示短 ID，摘要经 useDigest 批量到达后就地补一行字。
 */
export function IdChip({ kind, id, label }: { kind: string; id?: string | null; label?: string }) {
  const open = useContext(LinkContext);
  const digest = useDigest(kind, label ? null : id ?? null);
  if (!id) return <span className="muted">—</span>;
  return (
    <span className="id-chip" title={id} onClick={() => open(kind, id)}>
      <span>{label || shortId(id)}</span>
      {!label && digest ? <span className="id-note"> {digestLine(digest)}</span> : null}
    </span>
  );
}

/** 抽屉与摘要里的「键: 值」行。 */
export function ClaimLine({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="claim-line">
      <span className="st">{label}: </span>
      {children}
    </div>
  );
}
