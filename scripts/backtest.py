#!/usr/bin/env python3
"""主线统一回测/研究入口：把「联网单轮」与「多周闭环回放」收进一个 `--phase`。

合并自原 `scripts/research_run.py`（联网单轮：采集→建卡→冻结→研究图→委员会→OrderPlan，
以及 execute/review 手工触发）与 `scripts/full_flow_backtest.py`（从本地缓存 CSV 逐交易日
推进虚拟时钟，跑通 Fill→Episode→Outcome→Review→次周记忆引用 的诊断/回归链）。

五个环节（`--phase`，默认 `full`）：
  full      多周闭环回放：缓存 CSV 为准（逐 bar 落 PIT），周五决策→T+1 成交→到期结算→复盘→引用；
            全程真实 LLMClient，AcceptanceHarness 默认开（--no-force 关，关掉后不预置 seed episode）
  collect   只联网拉新闻/行情/披露入正文库（补缺口，不建卡、不决策、不跑模型）
  evidence  一个日频建卡轮：采集→建 Feature/EvidenceCard→入池，不冻结、不跑模型（= flow daily）
  decision  指定 --as-of 冻结 Snapshot→研究图→委员会→组合政策→OrderPlan（= flow decide + 注入模型）
  review    对挂起 OrderPlan 做 T+1 execute 成交，再扫描到期 Episode 结算 OutcomeCard→ReviewLabel

统一写生产账本 `config.RESEARCH_LEDGER_PATH`（测试期不设独立目录、不提供 --reset）；重跑靠
trace_prefix+as_of 秒级戳幂等、内容哈希 object_id 去重。force/seed 对象带 FORCED_ACCEPTANCE 标记。

`run_replay` 把多周闭环抽成一个只认 `sink(event_type, **fields)` 的驱动：终端与 viz SSE 是
同一事件流的两个 sink，不各写一份日循环（见 docs/Plan.md 4.4）。

用法（conda trader 环境）：
  python scripts/backtest.py                                          # full：默认回放（force 开）
  python scripts/backtest.py --pool AAPL --start 2026-02-06           # 换标的/换周一起点的闭环回放
  python scripts/backtest.py --no-force --pool AAPL                   # 纯真实判定的闭环（可能不产出 episode）
  python scripts/backtest.py --phase collect --pool NVDA              # 只联网补 NVDA 数据缺口
  python scripts/backtest.py --phase evidence --as-of 2026-10-09T20:00:00+00:00
  python scripts/backtest.py --phase decision --pool NVDA --as-of 2026-10-09T20:00:00+00:00
  python scripts/backtest.py --phase review --order-plan-id op_xxx --as-of 2026-11-01T20:00:00+00:00
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.flow import FlowOptions, run_research_round
from core.collect import filings as filings_collector
from core.collect import news as news_collector
from core.collect.market import (INDICATORS_VERSION, build_feature_card, load_price_frame,
                                 prepare_market_history)
from core.database import ResearchLedger, SnapshotBuilder
from core.database.contracts import (Fill, IntentItem, OrderPlan, PlannedOrder, PortfolioIntent,
                                     PositionEpisode)
from core.database.providers import canonical_content_hash
from core.database.raw import RawPayloadStore
from core.outcome import OutcomeScheduler
import config
from utils.llm_client import LLMClient

UTC = timezone.utc
PHASES = ("full", "collect", "evidence", "decision", "review")
# 与 core/collect 采集器、viz 一致的默认观察池（保持脚本间可对照）
DEFAULT_POOL = ["AAPL", "AMZN", "GOOGL", "JNJ", "JPM", "MSFT", "NVDA", "UNH", "V", "WMT"]
# 回放以本地缓存 CSV 为准，逐 bar 落 PIT，源版本钉死以便 SnapshotBuilder 校验
REPLAY_VERSION = "legacy-cache-replay-v1"
# 首个决策日常被 Risk Critic 软否决而无法自然开仓；force 模式下预置一笔 active episode，
# 让 outcome→review→记忆→引用 这条下游链确定性地跑通（开仓决策环节不参与）。


# ============================ 终端工具 ============================

def _enable_utf8_console() -> None:
    """Windows 控制台默认 GBK(936) 会把中文打成乱码；码页与 stdout 两层一起改才彻底。"""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def _hr(title: str) -> None:
    print(f"\n{'=' * 74}\n  {title}\n{'=' * 74}", flush=True)


def _clip(text: object, limit: int = 90) -> str:
    s = str(text).replace("\n", " ").strip()
    return s if len(s) <= limit else s[:limit] + "…"


# ============================ 终端观测包装（不属于 flow）============================

class TracingModel:
    """包一层 LLMClient：把每个研究节点的注入输入、工具调用、JSON 输出打到终端，行为不变。

    同时给每次调用注入 `audit` 关联键（node/symbol/as_of/call_id），透传给 `log_llm_call`
    落进 `logs/llm_calls.jsonl`，供后续审计 agent 把节点结论回溯到它当时看到的证据。
    """

    _AGENTS = ("event", "fundamental", "market")

    def __init__(self, client: LLMClient, symbol: str = "") -> None:
        self._c = client
        self._symbol = symbol

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
            if "portfolio_state" in data:
                return "committee"
        return message[:24]

    def _audit(self, node: str, data: dict | None) -> dict[str, Any]:
        symbol = str((data or {}).get("symbol") or self._symbol or "")
        return {"node": node, "symbol": symbol, "as_of": (data or {}).get("as_of"),
                "call_id": f"call_{uuid.uuid4().hex[:8]}"}

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
            state = data.get("portfolio_state") or {}
            print(f"  账户摘要    : portfolio_keys={list(state.keys())} regime={data.get('regime')}")
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
        kwargs["audit"] = self._audit(node, data)
        result = self._c.chat_json(message, **kwargs)
        self._show_output(node, result)
        return result

    def chat_with_tools(self, message: str, **kwargs):
        data = self._parse(message)
        node = self._node_of(data, message)
        self._show_input(node, data)
        kwargs["audit"] = self._audit(node, data)
        executor = kwargs["tool_executor"]

        def traced(name: str, arguments: dict) -> str:
            out = executor(name, arguments)
            self._show_tool(name, arguments, out)
            return out

        result = self._c.chat_with_tools(message, **dict(kwargs, tool_executor=traced))
        self._show_output(node, result)
        return result

    def record_forced(self, message: str, result: dict) -> dict:
        """force 验收轮的观测通道：不经 provider，也把注入/输出打进终端轨迹。"""
        data = self._parse(message)
        node = self._node_of(data, message)
        self._show_input(node, data)
        self._show_output(node, result)
        return result


class AcceptanceHarnessModel:
    """保留研究节点与 Risk Critic 的真实调用，只用确定性手段把 episode-记忆链跑通。

    这不是通用假模型：Event/Fundamental/Market/Risk Critic 全部走真实 provider。它只强制
    开仓首轮与后续引用所需的 Committee 选择，并替代 Review 的 memory_eligibility；Critic 的
    abstain/human_review 否决仍然生效。所有强制响应带 FORCED_ACCEPTANCE 终端标记，与模型判断区分。
    """

    def __init__(self, live: TracingModel) -> None:
        self._live = live

    @staticmethod
    def _prompt(message: str) -> dict[str, Any] | None:
        try:
            value = json.loads(message)
        except json.JSONDecodeError:
            try:
                value = ast.literal_eval(message)
            except (ValueError, SyntaxError):
                return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _forced_committee(prompt: dict[str, Any], *, purpose: str) -> dict[str, Any]:
        history = prompt.get("historical_episodes_by_symbol") or {}
        items = []
        for packet in prompt.get("packets", ()):
            symbol = str(packet["symbol"])
            claim_ids = [str(claim["claim_id"]) for claim in packet.get("claims", ())]
            episode_ids = [str(case["episode_id"]) for case in history.get(symbol, ())]
            critic = (packet.get("critic") or {}).get("verdict")
            # 保留生产 Critic 否决；引用对 abstain 也合法，能在不对抗 Risk Critic 的前提下
            # 证明「检索→委员会」接线，而不是凭空造一笔交易。
            if critic in {"abstain", "human_review"}:
                action, horizon_action, horizon_days, supporting = "abstain", "none", None, []
            else:
                action, horizon_action, horizon_days, supporting = "long", "start", 20, claim_ids[:1]
            items.append({
                "symbol": symbol,
                "action": action,
                "strength": 1,
                "priority": 1,
                "horizon_action": horizon_action,
                "horizon_days": horizon_days,
                "supporting_card_ids": supporting,
                "opposing_card_ids": [],
                "calibration_episode_ids": episode_ids[:1],
                "rationale": (
                    "[FORCED_ACCEPTANCE_OPEN] seed episode for structural replay; not an LLM judgement."
                    if purpose == "open"
                    else "[FORCED_ACCEPTANCE_CITATION] structural replay citation; not an LLM judgement."
                ),
                "no_trade_reason": "Risk Critic veto preserved by acceptance harness" if action == "abstain" else None,
            })
        return {"items": items}

    def chat_json(self, message: str, **kwargs: Any) -> dict[str, Any]:
        prompt = self._prompt(message)
        # Review 用 JSON，Committee 用 repr(dict)；此签名刻意收窄，Event/Fundamental/Market/Critic 保持真实调用
        if prompt is not None and "episode" in prompt and "outcome" in prompt:
            print("[FORCED ACCEPTANCE] Review memory_eligibility=eligible (structural replay only)", flush=True)
            return self._live.record_forced(message, {
                "reflection": "[FORCED_ACCEPTANCE_ELIGIBLE] Audited episode supplied to test downstream memory retrieval.",
                "memory_eligibility": "eligible",
            })
        if prompt is not None and "portfolio_state" in prompt and "packets" in prompt:
            if any(prompt.get("historical_episodes_by_symbol", {}).values()):
                print("[FORCED ACCEPTANCE] Committee historical citation injected (structural replay only)", flush=True)
                return self._live.record_forced(message, self._forced_committee(prompt, purpose="citation"))
            print("[FORCED ACCEPTANCE] Committee seed opening injected; Risk Critic veto remains binding", flush=True)
            return self._live.record_forced(message, self._forced_committee(prompt, purpose="open"))
        return self._live.chat_json(message, **kwargs)

    def chat_with_tools(self, message: str, **kwargs: Any) -> dict[str, Any]:
        return self._live.chat_with_tools(message, **kwargs)

    @property
    def provider(self) -> str:
        return self._live.provider

    @property
    def model(self) -> str:
        return self._live.model


# ============================ 缓存回放：取数（PIT 逐 bar）============================

def _at(day: date) -> datetime:
    """美股日线收盘后的固定可得时点；不把同日未来 bar 泄漏给早些时候。"""
    return datetime.combine(day, time(20), tzinfo=UTC)


def _business_days(start: date, end: date) -> list[date]:
    return [stamp.date() for stamp in pd.bdate_range(start, end)]


def _market_cache(symbol: str) -> Path:
    candidates = sorted((ROOT / "data" / "cache" / "market").glob(f"{symbol}_*.csv"),
                        key=lambda path: path.stat().st_size, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"missing market cache for {symbol}; run --phase collect to fetch it")
    return candidates[0]


def _store_market_history(store: RawPayloadStore, symbol: str, *, through: date) -> int:
    """逐 bar 建 raw batch，确保每个历史 as_of 只能读到当日及更早日线。"""
    count = 0
    with _market_cache(symbol).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            day = date.fromisoformat(row["Date"])
            if day > through:
                continue
            bar = {"date": day.isoformat(), **{key: float(row[key]) for key in ("open", "high", "low", "close", "volume")}}
            available = _at(day)
            payload = {
                "kind": "market", "type": "daily_bars", "symbol": symbol, "source": REPLAY_VERSION,
                "title": f"{symbol} cached daily bar {day.isoformat()}", "summary": "",
                "body": json.dumps([bar], sort_keys=True), "url": None,
                "window_start": day.isoformat(), "window_end": day.isoformat(),
                "published_at": available, "available_at": available,
                "provider_id": f"{REPLAY_VERSION}:{symbol}:{day.isoformat()}",
            }
            payload["content_hash"] = canonical_content_hash(payload)
            store.put(payload, source=REPLAY_VERSION, received_at=available)
            count += 1
    return count


def _news_by_day(pool: tuple[str, ...], start: date, end: date) -> dict[date, list[dict[str, Any]]]:
    records: dict[date, list[dict[str, Any]]] = defaultdict(list)
    directory = ROOT / "data" / "cache" / "news" / "complete"
    for symbol in pool:
        paths = sorted(directory.glob(f"{symbol}_news_seg_*.csv"))
        if not paths:
            raise FileNotFoundError(f"missing news cache for {symbol}; run --phase collect to fetch it")
        for path in paths:
            with path.open(newline="", encoding="utf-8") as handle:
                for ordinal, row in enumerate(csv.DictReader(handle), start=1):
                    published = datetime.fromisoformat(row["datetime"]).replace(tzinfo=UTC)
                    available_day = published.date()
                    # 收盘后和非交易日发布的新闻必须留到下一交易日导入，不能在当日 20:00 的
                    # Snapshot 中以未来信息出现；available_at 仍保留真实发布时点。
                    if published.time() > time(20) or available_day.weekday() >= 5:
                        available_day = (pd.Timestamp(available_day) + pd.offsets.BDay(1)).date()
                    if not start <= available_day <= end:
                        continue
                    record = {
                        "kind": "news", "symbol": symbol, "source": row.get("source") or REPLAY_VERSION,
                        "title": row.get("title", ""), "summary": row.get("summary", ""), "body": "",
                        "url": f"legacy-cache://{path.name}#{ordinal}", "published_at": published,
                        # 历史缓存不带接收时点：这是明示的回放近似 available_at = published_at。
                        "available_at": published, "provider_id": f"{path.name}:{ordinal}",
                        "sentiment_score": float(row.get("sentiment_score") or 0.0),
                    }
                    record["content_hash"] = canonical_content_hash(record)
                    records[available_day].append(record)
    return records


def _replay_account() -> dict[str, Any]:
    """回放用的自洽账户：满现金、无初始持仓，episode 只由成交/carry-in 生成。"""
    return {"cash": 100_000.0, "positions": [], "limits": {"max_position_ratio": 1.0,
            "max_single_loss_ratio": 0.02, "max_concentration": 0.35},
            "turnover_cost_estimate": {"per_side_bps": 10, "note": "backtest scenario"},
            "regime": "risk-on", "risk_state": {"drawdown": 0.0, "stop_loss": [], "risk_locked": False}}


def _apply_fills(account: dict[str, Any], fills) -> None:
    positions = {row["symbol"]: dict(row) for row in account["positions"]}
    for fill in fills:
        if fill.filled_shares <= 0:
            continue
        signed = fill.filled_shares if fill.side == "buy" else -fill.filled_shares
        account["cash"] -= signed * fill.executed_price + fill.commission
        row = positions.get(fill.symbol, {"symbol": fill.symbol, "quantity": 0, "avg_cost": fill.executed_price,
                                          "last_price": fill.executed_price})
        if signed > 0:
            previous = int(row["quantity"])
            row["avg_cost"] = ((previous * float(row["avg_cost"])) + signed * fill.executed_price) / (previous + signed)
        row["quantity"] = int(row["quantity"]) + signed
        row["last_price"] = fill.executed_price
        if row["quantity"] > 0:
            positions[fill.symbol] = row
        else:
            positions.pop(fill.symbol, None)
    account["positions"] = list(positions.values())


def _seed_episode(ledger: ResearchLedger, raw: RawPayloadStore, *, symbol: str,
                  decision_at: datetime, horizon_days: int, shares: int,
                  trace_prefix: str) -> PositionEpisode:
    """循环开始前预置一笔自洽的 active 开仓案例，用于验证 outcome→review→记忆→引用。

    不触碰 Risk Critic，也不替代任何研究节点：只沿 IntentItem/OrderPlan/Fill/PositionEpisode
    的引用链写入一笔已成交买入，让现有 OutcomeScheduler 与次周 Committee 把它当真实 episode
    结算与引用。执行价取决策日之后第一根真实日线的开盘价，与正式回放的 T+1 成本口径一致。
    """
    frame = load_price_frame(symbol, decision_at.strftime("%Y-%m-%d"),
                             (decision_at + timedelta(days=120)).strftime("%Y-%m-%d"), store=raw)
    later = frame.loc[frame.index > pd.Timestamp(decision_at.date())] if not frame.empty else frame
    if later.empty:
        raise RuntimeError(f"no market bar after the decision day to seed {symbol}")
    bar = later.iloc[0]
    executed_at = pd.Timestamp(bar.name).to_pydatetime().replace(tzinfo=UTC)
    executed_price = float(bar["open"])
    if executed_price <= 0.0:
        raise RuntimeError(f"seed bar carries a non-positive open price for {symbol}")
    due_at = (pd.Timestamp(executed_at) + pd.offsets.BDay(horizon_days)).to_pydatetime().replace(tzinfo=UTC)
    trace_id = f"{trace_prefix}-seed-{symbol}-{decision_at:%Y%m%d}"
    item = IntentItem(symbol=symbol, action="long", strength=1, priority=1,
                      horizon_action="start", horizon_days=horizon_days,
                      rationale="[FORCED_ACCEPTANCE_SEED] structurally seeded opening; not an LLM judgement.")
    intent = PortfolioIntent(trace_id=trace_id, snapshot_id=f"seed-snap-{symbol}", items=(item,),
                             created_at=executed_at)
    plan = OrderPlan(trace_id=trace_id, projected_portfolio_id=f"seed-rpp-{symbol}",
                     snapshot_id=f"seed-snap-{symbol}", orders=(PlannedOrder(symbol=symbol, side="buy",
                                                                            target_weight=0.1),),
                     created_at=executed_at)
    fill = Fill(trace_id=trace_id, order_plan_id=plan.order_plan_id, symbol=symbol, side="buy",
                decision_at=decision_at, executed_at=executed_at, requested_shares=shares,
                filled_shares=shares, executed_price=executed_price, commission=0.0, status="filled",
                created_at=executed_at)
    episode = PositionEpisode(trace_id=trace_id, symbol=symbol, source_trace_id=trace_id, status="active",
                              anchor_at=executed_at, current_due_at=due_at,
                              original_horizon_days=horizon_days, intent_item_ids=(item.intent_item_id,),
                              order_plan_ids=(plan.order_plan_id,), fill_ids=(fill.fill_id,),
                              created_at=executed_at)
    ledger.start_trace(trace_id, decision_at)
    ledger.append_intent(intent)
    ledger.append_order_plan(plan)
    ledger.append_fill(fill)
    ledger.append_position_episode(episode)
    ledger.complete_trace(trace_id, decision_at)
    return episode


# ============================ 多周闭环驱动（终端与 viz 共用）============================

# 审计对象的终端展示字段：kind 与 readmodel/账本对象名一致，前端才能直接按 /api/object 溯源
_OBJ_FIELDS = {
    "fill": ("fill_id", "symbol", "side", "requested_shares", "filled_shares",
             "executed_price", "commission", "status", "reason_codes"),
    "position_episode": ("episode_id", "symbol", "status", "anchor_at", "current_due_at",
                         "original_horizon_days", "outcome_card_id", "review_label_id",
                         "terminal_reason", "fill_ids"),
    "outcome_card": ("outcome_card_id", "episode_id", "symbol", "observation", "entry_price",
                     "mark_price", "gross_return", "net_return", "max_drawdown",
                     "holding_trading_days", "commission"),
    "review_label": ("review_label_id", "outcome_card_id", "status", "memory_eligibility",
                     "rationale", "reason_codes", "created_at"),
    "snapshot": ("snapshot_id", "as_of", "symbols"),
    "research_packet": ("packet_id", "symbol", "citation_coverage", "critic"),
    "portfolio_intent": ("intent_id", "trace_id"),
    "order_plan": ("order_plan_id", "trace_id"),
}


def _dump(obj: Any) -> dict[str, Any]:
    return obj.model_dump(mode="json") if hasattr(obj, "model_dump") else dict(vars(obj))


def run_replay(*, ledger: ResearchLedger, raw: RawPayloadStore, pool: tuple[str, ...],
               start: date, end: date, model: Any, account: dict[str, Any],
               sink: Callable[..., None], seed: bool = True,
               check_cancel: Callable[[], None] | None = None,
               trace_prefix: str = "backtest") -> dict[str, Any]:
    """从缓存 CSV 逐交易日推进闭环，把过程交给 `sink`；不新增研究/采集/执行逻辑。

    周五为决策日，T+1 撮合已冻结 OrderPlan，到期 Episode 由 OutcomeScheduler 结算并触发 Review；
    一个真实 Outcome 之后的下一周再跑一次决策，验证记忆引用接线。`seed=True` 时预置一笔 active
    episode 并对结果做结构性断言（force 模式）；`--no-force` 走纯真实判定，不 seed、不硬失败。
    """
    weekly_calendar = [day for day in _business_days(start, end) if day.weekday() == 4]
    if start not in weekly_calendar:
        raise ValueError("--start must be a Friday decision day")

    market_counts = {symbol: _store_market_history(raw, symbol, through=end) for symbol in pool}
    news = _news_by_day(pool, start, end)
    sink("log", stage="raw-import", day=start.isoformat(), market_bars=market_counts,
         news_days=len(news), version=REPLAY_VERSION)

    if seed:
        # 20:18 版就有这段预置，21:24 重写 backtest.py 时丢了调用：force 验收依赖
        # 窗口内有 active episode 可结算，不预置则重跑必然 INCOMPLETE（docstring 仍宣称预置）
        seeded = [_seed_episode(ledger, raw, symbol=symbol, decision_at=_at(start),
                                horizon_days=20, shares=300, trace_prefix=trace_prefix)
                  for symbol in pool]
        sink("log", stage="seed-episode", day=start.isoformat(),
             episodes=[episode.episode_id for episode in seeded])

    builder = SnapshotBuilder(ledger, raw)
    order_plans: list[tuple[date, str]] = []
    reviewed: list[PositionEpisode] = []
    final_intent: PortfolioIntent | None = None
    followup_decision: date | None = None
    last_day: date | None = None

    for day in _business_days(start, end):
        if check_cancel is not None:
            check_cancel()
        last_day = day
        as_of = _at(day)
        records = list(news.get(day, ()))
        is_decision_day = day == start or day == followup_decision
        if is_decision_day:
            for symbol in pool:
                feature = build_feature_card(symbol, as_of, store=raw)
                if feature is None:
                    raise RuntimeError(f"missing market feature for {symbol} at {day}")
                records.append(feature)
        cards = builder.ingest_records(trace_id=f"{trace_prefix}-ingest-{day:%Y%m%d}", as_of=as_of, records=records)
        sink("log", stage="daily-ingest", day=day.isoformat(), cards=len(cards))

        for decision_day, plan_id in list(order_plans):
            if day <= decision_day:
                continue
            execution = run_research_round(
                FlowOptions(pool=pool, mode="execute", as_of=as_of, account_state=account,
                            order_plan_id=plan_id, trace_prefix=trace_prefix),
                ledger=ledger, raw_store=raw)
            _apply_fills(account, execution.fills)
            sink("log", stage="execution", day=day.isoformat(), order_plan=plan_id,
                 fills=len(execution.fills), episodes=len(execution.episodes))
            for fill in execution.fills:
                sink("object", kind="fill", id=fill.fill_id, day=day.isoformat(), data=_dump(fill))
            for episode in execution.episodes:
                sink("object", kind="position_episode", id=episode.episode_id, day=day.isoformat(), data=_dump(episode))
            order_plans.remove((decision_day, plan_id))

        settled = OutcomeScheduler(ledger=ledger, raw_store=raw, model=model).scan(
            as_of=as_of, trace_prefix=f"{trace_prefix}-outcome")
        if settled.outcomes:
            reviewed.extend(settled.episodes)
            sink("log", stage="outcome-review", day=day.isoformat(), outcomes=len(settled.outcomes),
                 reviews=len(settled.review_labels),
                 eligibility=[label.memory_eligibility for label in settled.review_labels])
            for outcome in settled.outcomes:
                sink("object", kind="outcome_card", id=outcome.outcome_card_id, day=day.isoformat(), data=_dump(outcome))
            for label in settled.review_labels:
                sink("object", kind="review_label", id=label.review_label_id, day=day.isoformat(), data=_dump(label))
            for episode in settled.episodes:
                sink("object", kind="position_episode", id=episode.episode_id, day=day.isoformat(), data=_dump(episode))
            if followup_decision is None:
                candidates = [c for c in weekly_calendar if c > day]
                if candidates:
                    followup_decision = candidates[0]
                    sink("log", stage="followup-scheduled", day=day.isoformat(), decision_at=followup_decision)

        if not is_decision_day:
            continue
        result = run_research_round(
            FlowOptions(pool=pool, mode="decide", as_of=as_of, account_state=account,
                        news_days=7, trace_prefix=trace_prefix),
            model=model, ledger=ledger, raw_store=raw)
        final_intent = result.intent
        plan = result.portfolio.order_plan
        # hold/abstain 周仍有可审计 OrderPlan，但零订单不属于执行器的输入域
        if plan.orders:
            order_plans.append((day, plan.order_plan_id))
        sink("log", stage="weekly-decision", day=day.isoformat(), snapshot=result.snapshot.snapshot_id,
             packets=len(result.packets), intent=result.intent.intent_id, order_plan=plan.order_plan_id,
             orders=len(plan.orders), actions=[f"{item.symbol}:{item.action}" for item in result.intent.items])
        sink("object", kind="snapshot", id=result.snapshot.snapshot_id, day=day.isoformat(),
             data=result.snapshot.model_dump(mode="json"))
        for packet in result.packets:
            sink("object", kind="research_packet", id=packet.packet_id, day=day.isoformat(), data=_dump(packet))
        sink("object", kind="portfolio_intent", id=result.intent.intent_id, day=day.isoformat(),
             data=result.intent.model_dump(mode="json"))
        sink("object", kind="order_plan", id=plan.order_plan_id, day=day.isoformat(), data=plan.model_dump(mode="json"))
        if day == followup_decision:
            break

    verified = ledger.list_verified_memory_episodes()
    cited = [item for item in (final_intent.items if final_intent else ()) if item.calibration_episode_ids]
    memory_complete = bool(reviewed and verified and cited)
    with ledger._connect() as conn:
        retrievals = conn.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE tool_name='historical_episode_retrieval'").fetchone()[0]
    sink("log", stage="COMPLETE" if memory_complete else ("INCOMPLETE" if seed else "NATURAL-END"),
         day=(last_day or end).isoformat(), reviewed=[e.episode_id for e in reviewed],
         verified=[e.episode_id for e in verified],
         calibration={item.symbol: item.calibration_episode_ids for item in cited}, retrieval_audits=retrievals)
    if seed:
        # force 模式才把「闭环未产出可引用 episode」当硬失败；纯真实回放允许自然收口而不抛
        if retrievals < len(pool):
            raise AssertionError("committee retrieval audit records are missing")
        if not memory_complete:
            raise AssertionError("the replay did not produce an eligible episode and a later committee citation")
    return {"memory_complete": memory_complete, "reviewed": [e.episode_id for e in reviewed],
            "verified": [e.episode_id for e in verified], "retrieval_audits": retrievals}


def _terminal_sink(event_type: str, **fields: Any) -> None:
    """run_replay 的终端 sink：log 打成 `[day] stage k=v`，object 打审计字段摘要。"""
    day = fields.pop("day", None)
    if event_type == "log":
        stage = fields.pop("stage", "")
        rendered = " ".join(f"{k}={v}" for k, v in fields.items())
        print(f"[{day}] {stage} {rendered}", flush=True)
    elif event_type == "object":
        kind = fields["kind"]
        data = fields["data"]
        summary = {f: data.get(f) for f in _OBJ_FIELDS.get(kind, ())} or {"id": fields.get("id")}
        rendered = " ".join(f"{k}={_clip(v, 60)}" for k, v in summary.items())
        print(f"[{day}] {kind:16s} {rendered}", flush=True)


# ============================ 单轮结果播报 ============================

def _report(result, *, ledger_path: Path) -> None:
    print(f"\n[{result.mode}] as_of={result.as_of.isoformat()} 建卡={result.ingested} 张", flush=True)
    if result.fills:
        for fill in result.fills:
            print(f"  Fill {fill.symbol:6s} {fill.side:4s} status={fill.status} "
                  f"shares={fill.filled_shares}/{fill.requested_shares}")
    if result.mode == "execute":
        return
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
    if result.portfolio is None:
        return
    p = result.portfolio
    print(f"  OrderPlan {p.order_plan.order_plan_id}: orders={len(p.order_plan.orders)} "
          f"deferred={len(p.order_plan.deferred_trades)}")
    for order in p.order_plan.orders:
        print(f"    {order.side:4s} {order.symbol:6s} target={order.target_weight:.2%} forced={order.forced}")
    for item in p.order_plan.deferred_trades:
        print(f"    defer {item.symbol:6s} target={item.target_weight:.2%} reason={item.reason_code}")


# ============================ 命令行与环节派发 ============================

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="主线统一回测入口（full/collect/evidence/decision/review）")
    p.add_argument("--phase", choices=PHASES, default="full", help="环节，默认 full：多周闭环回放")
    p.add_argument("--pool", nargs="+", default=DEFAULT_POOL, help="标的池（默认 10 只观察池；full 回放建议 --pool AAPL 省 token）")
    p.add_argument("--start", default="2026-05-01", help="full：首个周度决策日（必须是周五，YYYY-MM-DD）")
    p.add_argument("--end", default=None, help="full：回放窗口结束日（默认 start+110 天的保护上界，实际在真实 Outcome 后下一周提前收口）")
    p.add_argument("--as-of", default=None, help="collect/evidence/decision/review 的决策时点 ISO-8601（full 忽略；缺省=实盘当前 UTC）")
    p.add_argument("--account", default=str(ROOT / "data" / "account.json"), help="decision/review 账户 JSON（须含 cash/positions/limits）")
    p.add_argument("--order-plan-id", default=None, help="review：对该 OrderPlan 先 T+1 execute 再扫描到期 episode")
    p.add_argument("--trigger", choices=("exit", "thesis_invalidated"), default=None, help="仅 review：显式记录退出或 thesis 失效")
    p.add_argument("--trigger-evidence-id", nargs="+", default=(), help="仅 thesis_invalidated：该 episode 原冻结 Snapshot 内的 EvidenceCard ID")
    p.add_argument("--days", type=int, default=7, help="新闻回看天数（collect/evidence/decision）")
    p.add_argument("--market-lookback", type=int, default=220, help="行情回看天数（collect/evidence/decision）")
    p.add_argument("--no-filings", dest="filings", action="store_false", help="不采 SEC 披露（collect/evidence/decision）")
    p.add_argument("--provider", default=None, help="LLM provider（默认取 config.DEFAULT_LLM_PROVIDER）")
    p.add_argument("--model", default=None, help="LLM model（默认取 config.DEFAULT_LLM_MODEL）")
    p.add_argument("--no-force", dest="force", action="store_false", help="full：关掉 AcceptanceHarness 与 seed，走纯真实判定")
    p.set_defaults(filings=True, force=True)
    return p.parse_args()


def _utc_or_parse(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise SystemExit("--as-of 必须带时区（例如 2026-10-03T18:00:00+00:00）")
    return parsed


def _load_account(path: str) -> dict:
    file = Path(path)
    if not file.is_file():
        raise SystemExit(f"账户文件不存在：{path}\ndecision/review 需要真实持仓（cash/positions/limits）供 Risk Critic 审计；"
                         "只想补数据请走 --phase collect / evidence。")
    raw = json.loads(file.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise SystemExit(f"--account 需要是一个对象：{path}")
    raw.setdefault("note", f"loaded from {path}")
    return raw


def _build_client(args: argparse.Namespace) -> LLMClient:
    provider = args.provider or config.DEFAULT_LLM_PROVIDER
    model = args.model or config.DEFAULT_LLM_MODEL
    return LLMClient(provider=provider, model=model)


def _phase_collect(args: argparse.Namespace, as_of: datetime | None) -> int:
    """只联网拉数据入正文库：news/filings 落 raw，行情整批入库。不建卡、不决策。"""
    raw = RawPayloadStore(config.RESEARCH_RAW_DIR)
    window = as_of or datetime.now(timezone.utc)
    end = window.strftime("%Y-%m-%d")
    news_start = (window - timedelta(days=args.days)).strftime("%Y-%m-%d")
    fresh = as_of is None
    news_total = filings_total = 0
    try:
        for symbol in args.pool:
            news = news_collector.fetch_news_records(symbol, news_start, end, store=raw, include_fresh=fresh)
            news_total += len(news)
            print(f"  [collect] {symbol:6s} news={len(news)}", flush=True)
            if args.filings:
                filings_total += len(filings_collector.fetch_filings_records(symbol, news_start, end))
            prepare_market_history(symbol, window, store=raw, include_fresh=fresh)
    finally:
        raw.close()
    print(f"[collect] 标的 {len(args.pool)} 只｜新闻 {news_total} 条｜披露 {filings_total} 条｜正文库：{config.RESEARCH_RAW_DIR}",
          flush=True)
    return 0


def _phase_evidence(args: argparse.Namespace, as_of: datetime | None) -> int:
    """一个日频建卡轮：flow daily 采集→建 Feature/EvidenceCard→入池，不冻结、不跑模型。"""
    options = FlowOptions(pool=tuple(args.pool), mode="daily", as_of=as_of, news_days=args.days,
                          market_lookback=args.market_lookback, filings=args.filings, trace_prefix="backtest-evidence")
    result = run_research_round(options)
    print(f"[evidence] as_of={result.as_of.isoformat()} 建卡入池 {result.ingested} 张（未冻结）｜账本：{config.RESEARCH_LEDGER_PATH}",
          flush=True)
    return 0


def _phase_decision(args: argparse.Namespace, as_of: datetime | None) -> int:
    """指定 as_of 跑一次 decide：池选卡冻结→研究图→委员会→组合政策→OrderPlan（真实 LLM）。"""
    account = _load_account(args.account)
    options = FlowOptions(pool=tuple(args.pool), mode="decide", as_of=as_of, news_days=args.days,
                          market_lookback=args.market_lookback, filings=args.filings,
                          account_state=account, trace_prefix="backtest-decision")
    client = _build_client(args)
    print(f"[decision] 标的 {len(options.pool)} 只｜账户 {args.account}｜provider={client.provider}/{client.model}"
          "；逐节点交互另见 logs/llm_calls.jsonl", flush=True)
    try:
        result = run_research_round(options, model=TracingModel(client))
    except ValueError as exc:
        print(f"[入口拒绝] {exc}", file=sys.stderr, flush=True)
        return 2
    _report(result, ledger_path=config.RESEARCH_LEDGER_PATH)
    return 0


def _phase_review(args: argparse.Namespace, as_of: datetime | None) -> int:
    """复盘：--order-plan-id 先做 T+1 execute 成交，再扫描到期 Episode 结算 OutcomeCard→ReviewLabel。"""
    ledger = ResearchLedger(config.RESEARCH_LEDGER_PATH)
    raw = RawPayloadStore(config.RESEARCH_RAW_DIR)
    client = _build_client(args)
    when = as_of or datetime.now(timezone.utc)
    try:
        if args.order_plan_id:
            account = _load_account(args.account)
            execution = run_research_round(
                FlowOptions(pool=tuple(args.pool), mode="execute", as_of=when, account_state=account,
                            order_plan_id=args.order_plan_id, trace_prefix="backtest-review-execute"),
                ledger=ledger, raw_store=raw)
            for fill in execution.fills:
                print(f"  Fill {fill.symbol:6s} {fill.side:4s} status={fill.status} "
                      f"shares={fill.filled_shares}/{fill.requested_shares} @ {fill.executed_price}")
            print(f"[review] execute order_plan={args.order_plan_id} fills={len(execution.fills)} "
                  f"episodes={len(execution.episodes)}", flush=True)
        if args.trigger and not args.order_plan_id:
            raise ValueError("--trigger requires --order-plan-id so the terminal episode is explicit")
        episode_ids = None
        if args.order_plan_id:
            episode_ids = tuple(e.episode_id for e in ledger.list_active_position_episodes()
                                if args.order_plan_id in e.order_plan_ids)
        reason = "early_exit" if args.trigger == "exit" else args.trigger
        result = OutcomeScheduler(ledger=ledger, raw_store=raw, model=TracingModel(client)).scan(
            as_of=when, trace_prefix="backtest-review", episode_ids=episode_ids, terminal_reason=reason,
            trigger_evidence_ids=tuple(args.trigger_evidence_id))
    except ValueError as exc:
        print(f"[entry rejected] {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        raw.close()
    print(f"[review] as_of={when.isoformat()} settled={len(result.outcomes)}", flush=True)
    for outcome, label in zip(result.outcomes, result.review_labels):
        print(f"  Outcome {outcome.symbol:6s} {outcome.observation}: actual net={outcome.net_return:.2%}")
        print(f"  Review {label.outcome_card_id}: status={label.status} eligibility={label.memory_eligibility}")
    return 0


def _phase_full(args: argparse.Namespace) -> int:
    """多周闭环回放：缓存 CSV 推进虚拟时钟，force 默认开（seed + AcceptanceHarness）。"""
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start + timedelta(days=110)
    pool = tuple(str(s).strip().upper() for s in dict.fromkeys(args.pool))
    ledger = ResearchLedger(config.RESEARCH_LEDGER_PATH)
    raw = RawPayloadStore(config.RESEARCH_RAW_DIR)
    live = TracingModel(_build_client(args))
    model = AcceptanceHarnessModel(live) if args.force else live
    print(f"[full] 标的 {pool}｜窗口 {start}→{end}｜force={'开' if args.force else '关'}｜"
          f"账本 {config.RESEARCH_LEDGER_PATH}", flush=True)
    try:
        summary = run_replay(ledger=ledger, raw=raw, pool=pool, start=start, end=end, model=model,
                             account=_replay_account(), sink=_terminal_sink, seed=args.force,
                             trace_prefix=f"backtest-full-{uuid.uuid4().hex[:8]}")
    except FileNotFoundError as exc:
        print(f"[缺缓存] {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        raw.close()
    return 0 if (summary["memory_complete"] or not args.force) else 1


def main() -> int:
    _enable_utf8_console()
    args = _parse_args()
    args.pool = [str(s).strip().upper() for s in args.pool if str(s).strip()]
    if not args.pool:
        raise SystemExit("--pool 至少一个标的")
    as_of = _utc_or_parse(args.as_of)
    if args.phase == "full":
        return _phase_full(args)
    if args.phase == "collect":
        return _phase_collect(args, as_of)
    if args.phase == "evidence":
        return _phase_evidence(args, as_of)
    if args.phase == "decision":
        return _phase_decision(args, as_of)
    return _phase_review(args, as_of)


if __name__ == "__main__":
    raise SystemExit(main())
