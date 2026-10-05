#!/usr/bin/env python3
"""研究链统一入口：一条命令跑完全程（采集 → 建卡 → 冻结 → 研究图 → 委员会）

编排本体在 `agents/research/flow.py`，本脚本只做参数装配与逐节点打印。命令行只暴露
「跑全程」这一个节奏（= flow 的 `collect` + 注入模型，一条命令走完采集→建卡→冻结→研究图→
委员会）。`daily`（只补数据、不冻结）与 `decide`（不采集、从卡片池冻结后决策）是给外部定时
调度器直接调用 `run_research_round` 用的，不再各自养一份命令行参数装配。

用法（conda trader 环境）：
  python scripts/research_run.py                                    # 全默认：10 只观察池 + data/account.json
  python scripts/research_run.py --pool NVDA AAPL                   # 只跑指定标的
  python scripts/research_run.py --account data/account.json        # 换账户文件
  python scripts/research_run.py --as-of 2026-10-03T18:00:00+00:00  # 历史时点回放（本轮新拉数据被 PIT 拒绝）

账户口径：全链要跑 Risk Critic，必须带真实持仓（`cash`/`positions`/`limits`）。缺账户时 flow
直接拒绝而不是退回占位值——Risk Critic 的 prompt 规定「缺 limits 或持仓状态不得当作放行」，
占位账户只会跑出一轮全 abstain 的意图，白烧模型还留下一条看起来成功的 trace。

trace_id 约定：trace_id 取自 as_of 的秒级戳，同一时点重复运行会被账本拒绝（防覆盖历史）；脚本把
这条 ValueError 当作退出码 2 的幂等信号，而不是崩溃。逐节点输入/工具/输出打到终端，原始调用
另见 logs/llm_calls.jsonl。
"""

import sys
from pathlib import Path

# 让直接运行（python scripts/xxx.py）也能找到 core 模块；保留 Path 类型，默认账户路径要用 `/` 拼
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import argparse
import ast
import json
from datetime import datetime

import config
from agents.research.flow import FlowOptions, run_research_round
from utils.llm_client import LLMClient

# 与 core/data 采集器、viz 一致的默认观察池（保持脚本间可对照）
DEFAULT_POOL = ["AAPL", "AMZN", "GOOGL", "JNJ", "JPM", "MSFT", "NVDA", "UNH", "V", "WMT"]


# ============================ 命令行 ============================

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="跑完整研究链：采集 -> 建卡 -> 冻结 -> 研究图 -> 委员会")
    p.add_argument("--pool", nargs="+", default=DEFAULT_POOL, help="标的池（默认 10 只观察池）")
    p.add_argument("--account", default=str(_PROJECT_ROOT / "data" / "account.json"),
                   help="账户状态 JSON 路径，必须含 cash/positions/limits")
    p.add_argument("--as-of", default=None, help="决策时点 ISO-8601（默认当前 UTC = 实盘）")
    return p.parse_args()


def _utc_or_parse(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise SystemExit("--as-of 必须带时区（例如 2026-10-03T18:00:00+00:00）")
    return parsed


def _load_account(path: str) -> dict:
    # utf-8-sig：PowerShell/记事本写出的 JSON 常带 BOM，纯 utf-8 读取会直接抱 JSONDecodeError。
    file = Path(path)
    if not file.is_file():
        raise SystemExit(f"账户文件不存在：{path}\n"
                         "全链需要真实持仓（cash/positions/limits）供 Risk Critic 审计；"
                         "只想补数据请直接调用 flow 的 mode=daily。")
    raw = json.loads(file.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise SystemExit(f"--account 需要是一个对象：{path}")
    # note 一类的说明字段不影响决策，但账户口径必须能被追溯，缺来源时补一个文件路径。
    raw.setdefault("note", f"loaded from {path}")
    return raw


def _enable_utf8_console() -> None:
    """Windows 控制台默认 GBK(936) 码页会把中文打印成乱码；切到 UTF-8 码页并让 stdout 同步。

    只重配 stdout 不够：若控制台码页仍是 GBK，PowerShell 会按 GBK 解码 UTF-8 字节、照样乱码。
    SetConsoleOutputCP(65001) 把输出码页也改为 UTF-8，两层一起才彻底免往手动 chcp。
    """
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


# ============================ 观测包装（不属于 flow）============================

def _hr(title: str) -> None:
    print(f"\n{'=' * 74}\n  {title}\n{'=' * 74}", flush=True)


def _clip(text: object, limit: int = 90) -> str:
    s = str(text).replace("\n", " ").strip()
    return s if len(s) <= limit else s[:limit] + "…"


class TracingModel:
    """包一层 LLMClient：把每个研究节点的注入输入、工具调用、JSON 输出打到终端，行为不变。"""

    _AGENTS = ("event", "fundamental", "market")

    def __init__(self, client: LLMClient) -> None:
        self._c = client

    @property
    def provider(self) -> str:
        return self._c.provider

    @property
    def model(self) -> str:
        return self._c.model

    @staticmethod
    def _parse(message: str) -> dict | None:
        try:
            value = ast.literal_eval(message)
            return value if isinstance(value, dict) else None
        except (ValueError, SyntaxError):
            return None

    @classmethod
    def _node_of(cls, data: dict | None, message: str) -> str:
        if isinstance(data, dict):
            if data.get("agent") in cls._AGENTS:
                return str(data["agent"])
            if "portfolio" in data:
                return "risk_critic"
            if "thesis_book" in data:
                return "committee"
        return message[:24]

    def _show_input(self, node: str, data: dict | None) -> None:
        _hr(f"[LLM 调用] 节点 = {node}")
        if not isinstance(data, dict):
            print("  (无法解析输入结构，原始消息见 llm log)")
            return
        if node in self._AGENTS:
            print(f"  instruction : {_clip(data.get('instruction'), 80)}")
            print(f"  symbol    : {data.get('symbol')}  as_of: {_clip(data.get('as_of'), 30)}")
            print(f"  upstream  : {data.get('upstream_claim_ids') or '(无)'}")
            evidence = data.get("evidence") or []
            print(f"  注入证据  : {len(evidence)} 条  ← 模型首轮即可见，无需再调工具去要")
            for rec in evidence[:3]:
                print(f"     · [{_clip(rec.get('evidence_id'), 12)}] {_clip(rec.get('title'), 60)}")
            if len(evidence) > 3:
                print(f"     · … 其余 {len(evidence) - 3} 条略")
            if data.get("context"):
                print(f"  附加上下文: {list(data['context'].keys())}")
        elif node == "risk_critic":
            print(f"  待审 claims : {len(data.get('claims', []))} 张")
            print(f"  注入上下文: portfolio={'有' if data.get('portfolio') else '空'} "
                  f"turnover={'有' if data.get('turnover_cost') else '空'} regime={data.get('regime') is not None} "
                  f"peer={len(data.get('peer_comparison') or {})} 组")
        elif node == "committee":
            book = data.get("thesis_book") or {}
            print(f"  ThesisBook  : packets={len(book.get('packet_ids', []))} regime={book.get('regime')} "
                  f"portfolio_keys={list((book.get('portfolio_state') or {}).keys())}")
            print(f"  标的摘要    : {len(data.get('packets', []))} 个 Packet digest")

    def _show_tool(self, name: str, arguments: dict, raw_out: str) -> None:
        print(f"  ⟐ 工具调用: {name}({json.dumps(arguments, ensure_ascii=False)})")
        try:
            out = json.loads(raw_out)
        except json.JSONDecodeError:
            print("     → (非 JSON 返回)")
            return
        if isinstance(out, dict) and "records" in out:
            recs = out["records"]
            note = out.get("note")
            print(f"     → {len(recs)} 条命中" + (f"，note: {_clip(note, 70)}" if note else ""))
        elif isinstance(out, list):
            ids = [r.get("evidence_id") for r in out if isinstance(r, dict)][:5]
            print(f"     → {len(out)} 条命中，evidence_id: {ids}")

    def _show_output(self, node: str, result: dict) -> None:
        print(f"  【{node} 输出】")
        if not isinstance(result, dict):
            print(f"     {result}")
            return
        if result.get("route") == "data_request":
            print(f"     路由 data_request：{_clip(result.get('detail'), 100)}")
            return
        if "claims" in result:
            for c in result["claims"]:
                print(f"     · [{_clip(c.get('stance'), 8)} conf={c.get('confidence')}] "
                      f"cited={len(c.get('supporting_evidence_ids', []))} → {_clip(c.get('statement'), 90)}")
            return
        if "verdict" in result:
            print(f"     verdict = {result.get('verdict')}  card_ids={len(result.get('card_ids', []))}")
            for r in result.get("reasons", [])[:4]:
                print(f"       - {_clip(r, 100)}")
            return
        if "items" in result:
            for it in result["items"]:
                print(f"     · {it.get('symbol'):6s} action={it.get('action'):8s} "
                      f"strength={it.get('strength')} → {_clip(it.get('rationale'), 70)}")
            return
        print(f"     keys={list(result.keys())}")

    def chat_json(self, message: str, **kwargs):
        data = self._parse(message)
        node = self._node_of(data, message)
        self._show_input(node, data)
        result = self._c.chat_json(message, **kwargs)
        self._show_output(node, result)
        return result

    def chat_with_tools(self, message: str, **kwargs):
        data = self._parse(message)
        node = self._node_of(data, message)
        self._show_input(node, data)
        executor = kwargs["tool_executor"]

        def traced(name: str, arguments: dict) -> str:
            out = executor(name, arguments)
            self._show_tool(name, arguments, out)
            return out

        result = self._c.chat_with_tools(message, **dict(kwargs, tool_executor=traced))
        self._show_output(node, result)
        return result


# ============================ 结果播报 ============================

def _report(result, *, ledger_path: Path) -> None:
    print(f"\n[{result.mode}] as_of={result.as_of.isoformat()} 建卡={result.ingested} 张", flush=True)
    if result.snapshot is None:
        print(f"  未冻结 Snapshot（每日节奏）；卡片已入池，账本：{ledger_path}")
        return
    print(f"  Snapshot {result.snapshot.snapshot_id}  evidence={result.evidence}  "
          f"symbols={','.join(result.snapshot.symbols)}")
    for packet in result.packets:
        print(f"  · {packet.symbol:6s} coverage={packet.citation_coverage:.0%} "
              f"critic={packet.critic.verdict:7s} claims={len(packet.claim_card_ids)}")
    if result.intent is None:
        return
    print(f"  PortfolioIntent {result.intent.intent_id} (trace={result.intent.trace_id})")
    for item in result.intent.items:
        print(f"    {item.symbol:6s} action={item.action:8s} strength={item.strength} "
              f"priority={item.priority} no_trade={_clip(item.no_trade_reason, 40)}")
        print(f"           rationale: {_clip(item.rationale, 100)}")


def main() -> int:
    _enable_utf8_console()
    args = _parse_args()
    account = _load_account(args.account)
    options = FlowOptions(pool=tuple(args.pool), mode="collect", as_of=_utc_or_parse(args.as_of),
                          account_state=account, trace_prefix="run")
    # 默认口径跟 config 和 viz 一致：裸 LLMClient() 会读环境变量 DEFAULT_LLM_PROVIDER（缺省 openai），
    # 与 config 里的 deepseek 对不上，直接抛「未配置 OPENAI_API_KEY」。显式传 config 默认值免这个坑。
    client = LLMClient(provider=config.DEFAULT_LLM_PROVIDER, model=config.DEFAULT_LLM_MODEL)
    print(f"[全程] provider={client.provider}/{client.model}｜标的 {len(options.pool)} 只｜"
          f"账户 {args.account}；逐节点交互另见 logs/llm_calls.jsonl", flush=True)

    try:
        result = run_research_round(options, model=TracingModel(client))
    except ValueError as exc:
        # 入口把关（账户字段不齐、同时点重跑撞 trace）是调度器要读的一句话，
        # 带非零退出码让外部任务能区分「本轮已记过账」与真崩溃。
        print(f"[入口拒绝] {exc}", file=sys.stderr, flush=True)
        return 2
    _report(result, ledger_path=config.RESEARCH_LEDGER_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
