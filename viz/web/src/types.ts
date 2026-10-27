/** 与服务端 `viz/server.py` 的契约：字段名必须与 `Run.emit()` 的 kwargs、以及各 handler 的响应键一致。 */

export interface ProviderConfig {
  models: string[];
  default_model: string;
}

export interface Meta {
  pool: string[];
  providers: Record<string, ProviderConfig>;
  run_modes: { value: string; label: string }[];
  stages: { value: string; label: string }[];
  default_account: Record<string, unknown>;
  defaults: { provider: string; model: string | null };
  versions: { news: string; market: string; filings: string };
  paths: { ledger: string; raw: string; runs: string; llm_log: string };
  server_time: string;
}

/** POST /api/run 的载荷，与 `RunOptions.from_payload` 一一对应。 */
export interface RunPayload {
  pool: string[];
  mode: string;
  provider: string;
  model: string | null;
  as_of: string | null;
  days: number;
  market_lookback: number;
  filings: boolean;
  run_research: boolean;
  run_committee: boolean;
  snapshot_id: string | null;
  start: string | null;
  end: string | null;
  force: boolean;
  order_plan_id: string | null;
  account_state: Record<string, unknown> | null;
}

export type StageStatus = "pending" | "running" | "done" | "skipped" | "failed";

/**
 * 事件载荷随 type 变化，这里把服务端实际发过的字段显式列出：
 * 拼错字段名时编译期就能发现，而不是在界面上静默显示为空。
 */
export interface RunEvent {
  seq: number;
  ts: string;
  type: string;
  // 阶段
  stage?: string;
  label?: string;
  summary?: Record<string, any>;
  reason?: string;
  duration_ms?: number | null;
  // 模型节点
  call_id?: string;
  node?: string;
  symbol?: string;
  model?: string;
  prompt?: Record<string, any>;
  evidence_count?: number;
  evidence_ids?: string[];
  upstream_claim_ids?: string[];
  output?: any;
  error?: string | null;
  // 工具调用
  tool?: string;
  arguments?: Record<string, any>;
  result_count?: number;
  note?: string;
  result_raw?: string;
  // 链路对象
  kind?: string;
  id?: string;
  data?: any;
  agent?: string;
  trace_id?: string;
  day?: string;
  // 日志
  level?: string;
  text?: string;
  provider?: string;
  replay_stage?: string;
  // 运行元信息
  run_id?: string;
  state?: string;
  created_at?: string;
  options?: RunPayload;
  account_state?: unknown;
  finished_at?: string;
  duration_s?: number;
}

/** 一次模型调用：node_input / tool_call* / node_output 按 call_id 归并。 */
export interface RunCall {
  key: string;
  node: string;
  symbol: string;
  tools: RunEvent[];
  input?: RunEvent;
  output?: RunEvent;
}

export interface VizObject {
  kind: string;
  id: string;
  data: any;
  symbol?: string;
  agent?: string;
  trace_id?: string;
  pending?: boolean;
}

export interface StageInfo {
  status: StageStatus;
  label: string;
  summary?: Record<string, any>;
  duration_ms?: number | null;
  reason?: string;
}

export interface Digest {
  kind: string;
  symbol?: string;
  label?: string;
  source?: string;
  stance?: string | null;
}

export interface RunDetail {
  run_id: string;
  state: string;
  error?: string | null;
  options?: RunPayload;
  events: RunEvent[];
}

export interface RunIndexRow {
  run_id: string;
  state: string;
  created_at: string;
  duration_s?: number | null;
  events: number;
  options?: { pool?: string[] };
}

export interface SnapshotRow {
  snapshot_id: string;
  as_of: string;
  symbols: string[];
  evidence: number;
}

export interface TraceRow {
  trace_id: string;
  created_at: string;
  status: string;
  kinds: Record<string, number>;
}

export interface TraceDetail {
  trace_id: string;
  objects: Record<string, any[]>;
  tool_calls?: any[];
  queries?: any[];
}

export interface ObjDetail {
  kind: string;
  id: string;
  data: any;
  links?: Record<string, any[]>;
  trace_id?: string;
  created_at?: string;
}
