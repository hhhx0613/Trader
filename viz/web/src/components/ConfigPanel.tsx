import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Meta, RunPayload, SnapshotRow } from "../types";

interface Props {
  meta: Meta | null;
  running: boolean;
  onRun: (payload: RunPayload) => void;
}

/**
 * 左侧配置面板。表单状态全部留在本组件内：只有点「运行」时才摊成一个
 * POST /api/run 载荷，父组件只关心 run_id 与事件流。
 */
export default function ConfigPanel({ meta, running, onRun }: Props) {
  const [pool, setPool] = useState<Set<string>>(() => new Set(["NVDA"]));
  const [extraSymbols, setExtraSymbols] = useState<string[]>([]);
  const [draftSymbol, setDraftSymbol] = useState("");

  const [mode, setMode] = useState("collect");
  const [provider, setProvider] = useState("deepseek");
  const [model, setModel] = useState("");
  const [asOf, setAsOf] = useState("");
  const [days, setDays] = useState("7");
  const [marketLookback, setMarketLookback] = useState("220");
  const [filings, setFilings] = useState(true);
  const [runResearch, setRunResearch] = useState(true);
  const [runCommittee, setRunCommittee] = useState(true);

  const [accountText, setAccountText] = useState("");
  const [snapshots, setSnapshots] = useState<SnapshotRow[]>([]);
  const [snapshotId, setSnapshotId] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [force, setForce] = useState(true);
  const [orderPlanId, setOrderPlanId] = useState("");

  const [error, setError] = useState<string | null>(null);

  const loadSnapshots = useCallback(async () => {
    try {
      const { snapshots: rows } = await api<{ snapshots: SnapshotRow[] }>("/api/snapshots?limit=40");
      setSnapshots(rows);
      setSnapshotId((current) => current || rows[0]?.snapshot_id || "");
    } catch (e) {
      setError(`读取 Snapshot 失败：${(e as Error).message}`);
    }
  }, []);

  useEffect(() => {
    if (!meta) return;
    setAccountText(JSON.stringify(meta.default_account, null, 2));
    setProvider(meta.defaults.provider);
    setModel(meta.defaults.model ?? "");
    setMode(meta.run_modes[0]?.value ?? "collect");
    loadSnapshots();
  }, [meta, loadSnapshots]);

  // 自定义标的与默认池合并展示：默认池来自 /api/meta，追加项只在本次会话有效
  const symbolList = useMemo(() => {
    const base = meta?.pool ?? [];
    return [...base, ...extraSymbols.filter((s) => !base.includes(s))];
  }, [meta, extraSymbols]);

  const providerConfig = meta?.providers?.[provider];

  function toggleSymbol(symbol: string) {
    setPool((current) => {
      const next = new Set(current);
      if (next.has(symbol)) next.delete(symbol);
      else next.add(symbol);
      return next;
    });
  }

  function addSymbol() {
    const symbol = draftSymbol.trim().toUpperCase();
    if (!symbol) return;
    setPool((current) => new Set(current).add(symbol));
    setExtraSymbols((current) => (current.includes(symbol) ? current : [...current, symbol]));
    setDraftSymbol("");
  }

  function submit() {
    setError(null);
    let account: Record<string, unknown> | null;
    try {
      account = JSON.parse(accountText || "null");
    } catch (e) {
      setError(`账户状态 JSON 不合法：${(e as Error).message}`);
      return;
    }
    if (pool.size === 0) {
      setError("至少勾选一个标的");
      return;
    }
    onRun({
      pool: [...pool],
      mode,
      provider,
      model: model.trim() || null,
      as_of: asOf.trim() || null,
      days: Number(days) || 7,
      market_lookback: Number(marketLookback) || 220,
      filings,
      run_research: runResearch,
      run_committee: runCommittee,
      snapshot_id: mode === "snapshot" ? snapshotId : null,
      start: start.trim() || null,
      end: end.trim() || null,
      force,
      order_plan_id: orderPlanId.trim() || null,
      account_state: account,
    });
  }

  return (
    <aside className="panel config">
      <section>
        <h2>1 · 标的池</h2>
        <div className="chip-grid">
          {symbolList.map((symbol) => (
            <button
              key={symbol}
              className={`chip-toggle${pool.has(symbol) ? " on" : ""}`}
              onClick={() => toggleSymbol(symbol)}
            >
              {symbol}
            </button>
          ))}
        </div>
        <div className="row inline">
          <input
            type="text"
            placeholder="追加标的，如 TSLA"
            maxLength={8}
            value={draftSymbol}
            onChange={(e) => setDraftSymbol(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") addSymbol();
            }}
          />
          <button className="ghost" onClick={addSymbol}>
            添加
          </button>
        </div>
        <p className="hint">
          已选 <span className="mono">{pool.size}</span> 只；快照内的标的才可跑研究图
        </p>
      </section>

      <section>
        <h2>2 · 数据来源</h2>
        <div className="stack">
          {(meta?.run_modes ?? []).map((option) => (
            <label className="check" key={option.value}>
              <input
                type="radio"
                name="source"
                value={option.value}
                checked={mode === option.value}
                onChange={() => setMode(option.value)}
              />
              <span>{option.label}</span>
            </label>
          ))}
        </div>

        {/* snapshot 与 full/review 各有专属参数：只在选中时展开，避免无关输入混进载荷 */}
        <div className={`row${mode === "snapshot" ? "" : " hidden"}`}>
          <select value={snapshotId} onChange={(e) => setSnapshotId(e.target.value)}>
            {snapshots.length === 0 ? <option value="">（账本中暂无 Snapshot）</option> : null}
            {snapshots.map((s) => (
              <option key={s.snapshot_id} value={s.snapshot_id}>
                {`${s.snapshot_id.slice(0, 14)} · ${new Date(s.as_of).toLocaleString("zh-CN", {
                  hour12: false,
                })} · ${s.symbols.join("/")} · ${s.evidence} 卡`}
              </option>
            ))}
          </select>
          <button className="ghost tiny" title="重新读取账本" onClick={loadSnapshots}>
            ↻
          </button>
        </div>

        <div className={`stack${mode === "full" || mode === "review" ? "" : " hidden"}`}>
          <div className="row">
            <label>
              回放起始（周五）
              <input
                type="text"
                placeholder="YYYYMMDD，如 20260501"
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </label>
            <label>
              回放结束（可空）
              <input
                type="text"
                placeholder="留空 = 起始+110 天"
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
            </label>
          </div>
          <label className="check">
            <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
            <span>
              force：预置 seed episode + 委员会强制开仓/引用（AcceptanceHarness，仅结构性回放，非模型判断；仅 full 生效）
            </span>
          </label>
          <label>
            复盘 OrderPlan ID（可空）
            <input
              type="text"
              placeholder="review：先对该 OrderPlan 做 T+1 execute 成交，再扫描到期 Episode"
              value={orderPlanId}
              onChange={(e) => setOrderPlanId(e.target.value)}
            />
          </label>
          <p className="hint">
            full 从本地缓存 CSV 逐交易日推进（需先有该标的 cache，缺档先跑「跑全程」）；force 默认开
          </p>
        </div>
      </section>

      <section>
        <h2>3 · 采集窗口与时点</h2>
        <div className="row">
          <label>
            新闻回看天数
            <input type="number" min={1} max={365} value={days} onChange={(e) => setDays(e.target.value)} />
          </label>
          <label>
            行情回看天数
            <input
              type="number"
              min={30}
              max={1000}
              value={marketLookback}
              onChange={(e) => setMarketLookback(e.target.value)}
            />
          </label>
        </div>
        <label>
          决策时点 as_of（ISO-8601）
          <input
            type="text"
            placeholder="留空 = 实盘当前 UTC；如 2026-10-03T18:00:00+00:00"
            value={asOf}
            onChange={(e) => setAsOf(e.target.value)}
          />
        </label>
        <p className="hint">
          “新闻回看天数”：跑全程=采集窗口，决策=从池选卡的 available_from
          窗口；“行情回看天数”仅跑全程采集时生效；as_of 留空=实盘（采完定时点），填历史时点则新拉数据被 PIT 闸门拒绝
        </p>
      </section>

      <section>
        <h2>4 · 模型</h2>
        <div className="row">
          <label>
            提供商
            <select value={provider} onChange={(e) => setProvider(e.target.value)}>
              {Object.entries(meta?.providers ?? {}).map(([name, config]) => (
                <option key={name} value={name}>
                  {`${name}（默认 ${config.default_model}）`}
                </option>
              ))}
            </select>
          </label>
          <label>
            模型
            <input
              type="text"
              list="modelList"
              placeholder="留空 = 提供商默认"
              value={model}
              onChange={(e) => setModel(e.target.value)}
            />
            <datalist id="modelList">
              {(providerConfig?.models ?? []).map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
          </label>
        </div>
      </section>

      <section>
        <h2>5 · 阶段开关</h2>
        <label className="check">
          <input type="checkbox" checked={filings} onChange={(e) => setFilings(e.target.checked)} />
          <span>采集 SEC 披露（关掉可演示 Fundamental 干净弃权；仅跑全程生效）</span>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={runResearch}
            onChange={(e) => setRunResearch(e.target.checked)}
          />
          <span>
            逐标的研究图（Event → Fundamental‖Market → Critic →
            Packet）；跑全程可关掉只做「采集→建卡→冻结」的数据验收轮，决策节奏必须开着（否则 flow 拒绝）
          </span>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={runCommittee}
            onChange={(e) => setRunCommittee(e.target.checked)}
          />
          <span>
            委员会（ThesisBook →
            PortfolioIntent）；跑全程时随研究图由 flow 一并触发，仅「复用 Snapshot」节奏可单独关掉只看研究图
          </span>
        </label>
      </section>

      <section>
        <h2>6 · 账户状态（冻结进 Snapshot）</h2>
        <textarea
          rows={9}
          spellCheck={false}
          value={accountText}
          onChange={(e) => setAccountText(e.target.value)}
        />
        <p className="hint">Critic 与委员会按这份持仓判敞口/集中度；改这里可观察否决与减仓结论的变化</p>
      </section>

      <div className="actions">
        <button className="primary" onClick={submit} disabled={running}>
          ▶ 运行
        </button>
        <span className="error">{error}</span>
      </div>

      <section className="meta">
        <h2>口径</h2>
        {meta ? (
          <div className="kv tiny">
            {[
              ["news", meta.versions.news],
              ["market", meta.versions.market],
              ["filings", meta.versions.filings],
              ["ledger", meta.paths.ledger],
              ["正文库", meta.paths.raw],
              ["运行流", meta.paths.runs],
              ["LLM 日志", meta.paths.llm_log],
            ].map(([k, v]) => (
              <FragmentRow key={k} label={k} value={String(v)} />
            ))}
          </div>
        ) : null}
      </section>
    </aside>
  );
}

function FragmentRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <b>{label}</b>
      <span className="mono">{value}</span>
    </>
  );
}
