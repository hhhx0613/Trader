import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { OBJECT_ID_KEY, clip, fmtTime, shortId } from "../lib";
import type { RunIndexRow, TraceDetail, TraceRow, VizObject } from "../types";
import { ObjectGroup } from "./ObjectsTab";
import { Badge, IdChip } from "./ui";

interface Props {
  /** 由 trace chip 点击带过来：切到回放页并自动打开该 trace。 */
  focusTrace: string | null;
  onFocusConsumed: () => void;
  onReplayRun: (runId: string) => void;
}

/** 账本回放：对象 + 工具调用 + Gateway 查询审计（不含模型原始输入输出，那部分只在事件流里）。 */
export default function ReplayTab({ focusTrace, onFocusConsumed, onReplayRun }: Props) {
  const [traces, setTraces] = useState<TraceRow[]>([]);
  const [runs, setRuns] = useState<RunIndexRow[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const replayTrace = useCallback(async (traceId: string) => {
    if (!traceId) return;
    setLoading(true);
    setError(null);
    try {
      setDetail(await api<TraceDetail>(`/api/trace?trace_id=${encodeURIComponent(traceId)}`));
    } catch (e) {
      setDetail(null);
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadTraces = useCallback(async () => {
    setError(null);
    try {
      const [{ traces: rows }, { runs: runRows }] = await Promise.all([
        api<{ traces: TraceRow[] }>("/api/traces?limit=60"),
        api<{ runs: RunIndexRow[] }>("/api/runs?limit=40"),
      ]);
      setTraces(rows);
      setRuns(runRows);
      if (rows.length) {
        setSelected(rows[0].trace_id);
        replayTrace(rows[0].trace_id);
      } else {
        setDetail(null);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }, [replayTrace]);

  useEffect(() => {
    loadTraces();
  }, [loadTraces]);

  // trace chip 跳转：可能还不在下拉列表里（例如本轮刚产生），先补一个 option 再查
  useEffect(() => {
    if (!focusTrace) return;
    setSelected(focusTrace);
    replayTrace(focusTrace);
    onFocusConsumed();
  }, [focusTrace, replayTrace, onFocusConsumed]);

  return (
    <>
      <div className="row inline">
        <button className="ghost" onClick={loadTraces}>
          载入最近 trace
        </button>
        <select value={selected} onChange={(e) => replayTrace(e.target.value)}>
          {focusTrace && !traces.some((t) => t.trace_id === focusTrace) ? (
            <option value={focusTrace}>{focusTrace}</option>
          ) : null}
          {traces.map((trace) => (
            <option key={trace.trace_id} value={trace.trace_id}>
              {`${fmtTime(trace.created_at)} · ${trace.status} · ${
                trace.trace_id.length > 34 ? trace.trace_id.slice(0, 34) + "…" : trace.trace_id
              } · 卡${trace.kinds.evidence_card} 论${trace.kinds.claim_card} 包${trace.kinds.research_packet}`}
            </option>
          ))}
        </select>
        <span className="muted tiny">回放读账本：对象 + 工具调用 + Gateway 查询审计（不含模型原始输入输出）</span>
      </div>

      {error ? <div className="error">{`读取失败：${error}`}</div> : null}
      {loading ? <div className="muted tiny">读取中…</div> : null}

      {detail ? (
        <>
          <h3 className="mono">trace {detail.trace_id}</h3>
          {Object.entries(detail.objects ?? {}).map(([kind, list]) =>
            list.length ? (
              <ObjectGroup
                key={kind}
                kind={kind}
                objects={list.map((data) => toVizObject(kind, data))}
              />
            ) : null,
          )}
          {detail.tool_calls?.length ? (
            <div className="obj-group">
              <h3>{`模型补查审计 · ${detail.tool_calls.length}`}</h3>
              <table className="obj">
                <thead>
                  <tr>
                    {["节点", "工具", "参数", "命中", "证据"].map((column) => (
                      <th key={column}>{column}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {detail.tool_calls.map((call, index) => (
                    <tr key={index}>
                      <td>{call.node}</td>
                      <td>{call.tool}</td>
                      <td>{clip(call.arguments, 70)}</td>
                      <td>{call.result_count}</td>
                      <td>
                        {(call.evidence_ids ?? []).map((id: string) => (
                          <IdChip key={id} kind="evidence_card" id={id} label={shortId(id)} />
                        ))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          {detail.queries?.length ? (
            <div className="obj-group">
              <h3>{`Gateway 读取审计 · ${detail.queries.length} 次`}</h3>
              <div className="kv tiny">
                {Object.entries(
                  detail.queries.reduce<Record<string, number>>((acc, query) => {
                    const key = `${query.symbol}/${query.kind}`;
                    acc[key] = (acc[key] ?? 0) + 1;
                    return acc;
                  }, {}),
                ).map(([key, count]) => (
                  <FragmentText key={key} text={`${key}×${count}`} />
                ))}
              </div>
            </div>
          ) : null}
        </>
      ) : !loading && !error ? (
        <div className="empty">账本中还没有可回放的 trace</div>
      ) : null}

      <div className="obj-group">
        <h3>{`历史运行（本机 JSONL，共 ${runs.length}）`}</h3>
        {runs.length ? (
          <table className="obj">
            <thead>
              <tr>
                {["时间", "状态", "标的", "事件", "耗时", "操作"].map((column) => (
                  <th key={column}>{column}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_id}>
                  <td>{new Date(run.created_at).toLocaleString("zh-CN", { hour12: false })}</td>
                  <td>
                    <Badge kind={run.state === "done" ? "done" : run.state === "failed" ? "failed" : ""}>
                      {run.state}
                    </Badge>
                  </td>
                  <td>{(run.options?.pool ?? []).join("/")}</td>
                  <td>{run.events}</td>
                  <td>{run.duration_s ?? ""}</td>
                  <td>
                    <button className="ghost tiny" onClick={() => onReplayRun(run.run_id)}>
                      载入事件
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">还没有运行记录</div>
        )}
      </div>
    </>
  );
}

function toVizObject(kind: string, data: any): VizObject {
  const idKey = OBJECT_ID_KEY[kind] ?? "object_id";
  return { kind, id: String(data?.[idKey] ?? ""), data };
}

function FragmentText({ text }: { text: string }) {
  return <span>{text}</span>;
}
