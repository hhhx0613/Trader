#!/usr/bin/env python3
"""研究链路可视化调试台 / Local read-only HTTP + SSE front for the research flow.

一个后端文件，三段职责：
  1. 运行观测：把 `agents/research/flow.py` 的一次调用摊成前端事件——阶段产出取自
     `RoundResult`，逐节点的注入输入/工具调用/JSON 输出取自套在 `LLMClient` 外的 TracingModel。
  2. 只读查询：账本与正文库一律走 `viz/readmodel.py`，不写任何生产存储。
  3. 传输：标准库 HTTP + SSE，只监听 127.0.0.1，前端为无构建的原生页面。

这里不实现任何研究逻辑，也不复制阶段门控：「跑全程」就是调 `run_research_round`，与命令行
`scripts/research_run.py`、外部定时调度器用的是同一个函数。唯一保留的直接调用是 flow 没有的
节奏——复用已冻结 Snapshot 只重跑研究图/委员会，用于不重烧采集配额地反复调试研究节点。
职责边界见 docs/Plan.md 3.1.1 与 4.4。

用法（conda trader 环境）：
  python viz/server.py                            # 默认 http://127.0.0.1:8765
  python viz/server.py --port 9000                # 换端口
  python viz/server.py --runs-dir tmp/viz_runs    # 换观测产物目录
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config
from agents.research.committee import Committee
from agents.research.flow import FlowOptions, run_research_round
from agents.research.graph import PerAssetResearchGraph
from core.data import filings as filings_collector
from core.data import news as news_collector
from core.data.market import INDICATORS_VERSION
from core.research import DataGateway
from core.research.raw import RawPayloadStore
from core.research.store import ResearchLedger
from utils.llm_client import LLMClient
from viz.readmodel import LedgerReader

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_RUNS_DIR = config.PROJECT_ROOT / "output" / "viz_runs"

# 与 flow/命令行同一份默认观察池，保证前端勾选与命令行口径一致
DEFAULT_POOL = ["AAPL", "AMZN", "GOOGL", "JNJ", "JPM", "MSFT", "NVDA", "UNH", "V", "WMT"]

STAGE_LABELS = {
    # 编号对齐 docs/development_plan.md：阶段 1 采集/建卡/冻结，2 逐标的研究，3 委员会，4 组合政策
    "collect": "阶段 1 · 采集原始数据",
    "ingest": "阶段 1 · 建卡",
    "freeze": "阶段 1 · 冻结 Snapshot",
    "research": "阶段 2 · 逐标的研究图",
    "committee": "阶段 3 · 委员会",
    "portfolio": "阶段 4 · 组合政策",
}

# 数据来源三节奏，与 flow 对齐：collect/decide 走 run_research_round，snapshot 走生产原语
RUN_MODES = {
    "collect": "跑全程：联网采集 → 建卡 → 用本轮卡集点名冻结 → 研究图 → 委员会 → 组合政策",
    "decide": "决策：不联网，从已入池卡片按 available_from 窗口选卡冻结 → 研究图 → 委员会 → 组合政策（需真实账户）",
    "snapshot": "调试：复用已冻结 Snapshot，只重跑研究图/委员会（不采集不冻结，不烧采集配额）",
}

# 演示账户：Critic/Committee 要有真实敞口可审才会给方向性结论，而不是占位弃权。
# 前端可改这段 JSON 观察不同持仓下 Critic 与委员会的判定变化。
DEFAULT_ACCOUNT: dict[str, Any] = {
    "note": "viz 演示账户：组合模块落地后由真实持仓替换",
    "cash": 35_000.0,
    "positions": [
        {"symbol": "NVDA", "quantity": 120, "avg_cost": 180.0, "last_price": 233.95},
        {"symbol": "AAPL", "quantity": 200, "avg_cost": 210.0, "last_price": 232.0},
    ],
    "limits": {"max_position_ratio": config.MAX_POSITION_RATIO,
               "max_single_loss_ratio": config.MAX_SINGLE_LOSS_RATIO,
               "max_concentration": 0.35},
    "turnover_cost_estimate": {"per_side_bps": 10, "note": "演示值，口径参照 config 的手续费+滑点"},
    "regime": "risk-on: mega-cap tech uptrend, realized vol below 1-year average",
}

_DEFAULTS = {name: LLMClient.PROVIDERS[name] for name in ("deepseek", "glm", "openai")}
_CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                  ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
                  ".svg": "image/svg+xml", ".ico": "image/x-icon"}

# 单条事件里正文的截断长度：UI 要看的是「模型收到了什么」，全文另有 evidence 详情接口
_TEXT_CLIP = 320
# 一次阶段最多逐张外发的卡片事件数，其余只进批次汇总，避免千张卡把事件流灌满
_MAX_CARD_EVENTS = 120


# ============================ 小工具 ============================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clip_text(value: str) -> str:
    return value if len(value) <= _TEXT_CLIP else value[:_TEXT_CLIP] + "…（已截断，全文按 evidence_id 查正文库）"


def _clip_tree(value: Any, *, key: str | None = None) -> Any:
    """递归裁剪 prompt 结构里的长正文，保留全部 ID 与字段供溯源。"""
    if isinstance(value, dict):
        return {k: _clip_tree(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip_tree(v, key=key) for v in value]
    if isinstance(value, str) and key in {"text", "body", "summary", "rationale", "statement", "detail"}:
        return _clip_text(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _ms_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


# 节点识别：与 scripts/research_run.py 的终端打印包装同构，改一处要一起看另一处，
# 否则同一轮运行会在终端和前端指向不同节点。
_RESEARCH_AGENTS = ("event", "fundamental", "market")


def _parse_prompt(message: str) -> dict | None:
    """研究节点的注入 payload 是 dict 字面量字符串；解析不出就当原始文本处理。"""
    try:
        value = ast.literal_eval(message)
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, dict) else None


def _detect_node(data: dict | None, *, fallback: str = "unknown") -> str:
    """Critic 与 Committee 不自报身份，只能按各自的独占上下文键识别。"""
    if isinstance(data, dict):
        if data.get("agent") in _RESEARCH_AGENTS:
            return str(data["agent"])
        if "portfolio" in data:
            return "risk_critic"
        if "thesis_book" in data:
            return "committee"
    return fallback


def _tool_output_view(raw: str) -> dict[str, Any]:
    """把工具返回翻成观测字段：命中数、命中证据 ID、截断提示或错误。"""
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return {"count": None, "raw": raw}
    if isinstance(out, dict) and "records" in out:
        records = out["records"] or []
        return {"count": len(records),
                "evidence_ids": [r.get("evidence_id") for r in records if isinstance(r, dict)],
                "note": out.get("note")}
    if isinstance(out, list):
        return {"count": len(out), "evidence_ids": [r.get("evidence_id") for r in out if isinstance(r, dict)]}
    if isinstance(out, dict) and "error" in out:
        return {"count": 0, "error": out}
    return {"count": 1, "raw": str(out)}


# ============================ 运行配置与事件总线 ============================

class RunCancelled(Exception):
    """用户在节点边界请求停止：只在中断点抛异常，绝不在模型调用中途硬切。"""


def _to_int(value: Any, default: int, *, minimum: int) -> int:
    """前端送的是数字，但手改/旧标签可能给字符串或空值：兜到合法下界而不是让 int() 抛。"""
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


@dataclass
class RunOptions:
    pool: tuple[str, ...] = ("NVDA",)
    mode: str = "collect"
    provider: str = "deepseek"
    model: str | None = None
    as_of: str | None = None
    snapshot_id: str | None = None
    days: int = 7                   # 新闻回看天数：仅跑全程的采集节奏生效（flow 的 news_days）
    market_lookback: int = 220      # 行情回看天数：同上，只影响本轮采集窗口
    filings: bool = True            # 是否采集 SEC 披露：关掉可演示 Fundamental 干净弃权
    run_research: bool = True       # 关掉 = 只做采集→建卡→冻结的数据验收轮（不给 flow 注入模型）
    run_committee: bool = True      # 仅 snapshot 调试节奏有意义：全链由 flow 一次跑到委员会

    @classmethod
    def from_payload(cls, body: dict[str, Any]) -> "RunOptions":
        pool = tuple(dict.fromkeys(str(s).strip().upper() for s in (body.get("pool") or []) if str(s).strip()))
        if not pool:
            raise ValueError("至少勾选一个标的")
        mode = str(body.get("mode") or "collect")
        if mode not in RUN_MODES:
            raise ValueError(f"未知运行模式：{mode}")
        snapshot_id = str(body.get("snapshot_id") or "").strip() or None
        if mode == "snapshot" and not snapshot_id:
            raise ValueError("复用 Snapshot 模式必须先选一个已冻结 Snapshot")
        as_of = str(body.get("as_of") or "").strip() or None
        if as_of:
            cls._parse_as_of(as_of)  # 先校验格式，让报错直接显示在前端而不是等线程抛异常
        return cls(pool=pool, mode=mode, provider=str(body.get("provider") or "deepseek"),
                   model=(str(body["model"]).strip() or None) if body.get("model") else None,
                   as_of=as_of, snapshot_id=snapshot_id,
                   days=_to_int(body.get("days"), 7, minimum=1),
                   market_lookback=_to_int(body.get("market_lookback"), 220, minimum=30),
                   filings=bool(body.get("filings", True)),
                   run_research=bool(body.get("run_research", True)),
                   run_committee=bool(body.get("run_committee", True)))

    @staticmethod
    def _parse_as_of(raw: str) -> datetime:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def flow_as_of(self) -> datetime | None:
        """留空 = 实盘：由 flow 在采集完成后才定时点，本轮到达的数据本轮即可用。"""
        return self._parse_as_of(self.as_of) if self.as_of else None


class Run:
    """一次运行的事件总线：内存追加 + JSONL 落盘 + 条件变量唤醒 SSE 订阅者。"""

    def __init__(self, run_id: str, options: RunOptions, runs_dir: Path, account_state: dict[str, Any]) -> None:
        self.run_id = run_id
        self.options = options
        self.account_state = account_state
        self.created_at = utc_now()
        self.path = Path(runs_dir) / f"{run_id}.jsonl"
        self.events: list[dict[str, Any]] = []
        self.state = "running"
        self.error: str | None = None
        self.finished_at: datetime | None = None
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._cancelled = threading.Event()
        self._marks: dict[str, datetime] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.emit("run_meta", run_id=run_id, state=self.state, created_at=self.created_at.isoformat(),
                  options={**options.__dict__, "pool": list(options.pool)}, account_state=account_state)

    def cancel(self) -> None:
        """只置标志；观测层在下一个节点调用前抛出，绝不在模型调用中途硬切。"""
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check_cancel(self) -> None:
        if self.cancelled:
            raise RunCancelled()

    def mark(self, name: str) -> None:
        """记录一个真实时刻，用于把 flow 的一次调用切回若干阶段的耗时。"""
        with self._cond:
            self._marks.setdefault(name, utc_now())

    def mark_time(self, name: str) -> datetime | None:
        with self._cond:
            return self._marks.get(name)

    def emit(self, event_type: str, **fields: Any) -> dict[str, Any]:
        """追加一个事件。

        seq 与落盘必须在同一把锁内完成：Fundamental 与 Market 在 LangGraph 里
        真并发，先取 len() 再 append 会让两个节点拿到同一个 seq，前端排序随之错乱。
        """
        event = {"ts": utc_now().isoformat(), "type": event_type}
        event.update({k: v for k, v in fields.items() if v is not None})
        with self._cond:
            event["seq"] = len(self.events)
            self.events.append(event)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            self._cond.notify_all()
        return event

    def stage_start(self, stage: str, **fields: Any) -> None:
        self.emit("stage_start", stage=stage, label=STAGE_LABELS[stage], **fields)

    def stage_done(self, stage: str, summary: dict[str, Any], *, duration_ms: int | None = None) -> None:
        self.emit("stage_done", stage=stage, label=STAGE_LABELS[stage], summary=summary,
                  duration_ms=duration_ms)

    def stage_skip(self, stage: str, reason: str) -> None:
        self.emit("stage_skip", stage=stage, label=STAGE_LABELS[stage], reason=reason)

    def log(self, text: str, level: str = "info", **fields: Any) -> None:
        self.emit("log", level=level, text=text, **fields)

    def object(self, kind: str, object_id: str, data: Any, *, clip: bool = False, **fields: Any) -> None:
        """外发一个链路对象。

        契约对象（卡片/论点/Packet/意图）原样外发，前端才能显示全文与引用链；
        只有携带大段正文的采集记录才截断，全文按 evidence_id 走正文库接口。
        """
        self.emit("object", kind=kind, id=object_id, data=_clip_tree(data) if clip else data, **fields)

    def finish(self, state: str, error: str | None = None) -> None:
        self.state = state
        self.error = error
        self.finished_at = utc_now()
        self.emit("run_end", state=state, error=error,
                  finished_at=self.finished_at.isoformat(),
                  duration_s=round((self.finished_at - self.created_at).total_seconds(), 1))

    def since(self, after_seq: int) -> list[dict[str, Any]]:
        with self._cond:
            return [e for e in self.events if e["seq"] > after_seq]

    def wait(self, after_seq: int, timeout: float = 15.0) -> list[dict[str, Any]]:
        """阻塞等待新事件；SSE 用它实现「无事件时挂住连接」而非轮询。"""
        with self._cond:
            if not [e for e in self.events if e["seq"] > after_seq] and self.state == "running":
                self._cond.wait(timeout)
        return [e for e in self.events if e["seq"] > after_seq]


def load_run_events(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """读取历史运行的 JSONL：返回 (run_meta, events)，供刷新后回看。"""
    meta: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "run_meta":
                meta = event
            else:
                events.append(event)
    return meta, events


# ============================ 模型观测包装 ============================

class TracingModel:
    """包一层 LLMClient：把每个节点的注入输入、工具调用、JSON 输出转成事件，行为不变。"""

    def __init__(self, run: Run, client: LLMClient, symbol: str = "") -> None:
        self._run = run
        self._c = client
        self._symbol = symbol

    @property
    def provider(self) -> str:
        return self._c.provider

    @property
    def model(self) -> str:
        return self._c.model

    def _open_call(self, message: str) -> tuple[str, str, str]:
        data = _parse_prompt(message)
        node = _detect_node(data, fallback=f"unknown:{list(data)[:3] if data else ''}")
        records = [item for item in ((data or {}).get("evidence") or []) if isinstance(item, dict)]
        run = self._run
        run.mark("first_model")
        if node == "committee":
            run.mark("committee")
        run.check_cancel()  # 停止只在节点边界生效：不半途丢一次已付费的模型调用
        call_id = f"call_{uuid.uuid4().hex[:8]}"
        symbol = str((data or {}).get("symbol") or self._symbol or "")
        run.emit("node_input", call_id=call_id, node=node, symbol=symbol,
                 model=f"{self.provider}/{self.model}",
                 prompt=_clip_tree(data) if data is not None else {"raw_message": _clip_text(message)},
                 evidence_count=len(records),
                 evidence_ids=[str(r["evidence_id"]) for r in records if r.get("evidence_id")],
                 upstream_claim_ids=(data or {}).get("upstream_claim_ids") or [])
        return call_id, node, symbol

    def _close_call(self, call_id: str, node: str, symbol: str, started: float, result: Any, error: str | None) -> None:
        self._run.emit("node_output", call_id=call_id, node=node, symbol=symbol,
                       duration_ms=int((time.monotonic() - started) * 1000),
                       output=_clip_tree(result) if isinstance(result, (dict, list)) else result,
                       error=error)

    def _emit_tool(self, call_id: str, node: str, symbol: str, name: str, arguments: dict, raw_out: str) -> None:
        viewed = _tool_output_view(raw_out)
        fields: dict[str, Any] = {"call_id": call_id, "node": node, "symbol": symbol, "tool": name,
                                  "arguments": arguments, "result_count": viewed.get("count", 0)}
        if viewed.get("evidence_ids"):
            fields["evidence_ids"] = viewed["evidence_ids"]
        if viewed.get("note"):
            fields["note"] = viewed["note"]
        if viewed.get("error"):
            fields["error"] = _clip_tree(viewed["error"])
        if viewed.get("raw"):
            fields["result_raw"] = _clip_text(viewed["raw"])
        self._run.emit("tool_call", **fields)

    def chat_json(self, message: str, **kwargs):
        call_id, node, symbol = self._open_call(message)
        started = time.monotonic()
        try:
            result = self._c.chat_json(message, **kwargs)
        except Exception as exc:
            self._close_call(call_id, node, symbol, started, None, f"{type(exc).__name__}: {exc}")
            raise
        self._close_call(call_id, node, symbol, started, result, None)
        return result

    def chat_with_tools(self, message: str, **kwargs):
        call_id, node, symbol = self._open_call(message)
        executor = kwargs["tool_executor"]

        def traced(name: str, arguments: dict) -> str:
            out = executor(name, arguments)
            self._emit_tool(call_id, node, symbol, name, arguments, out)
            return out

        started = time.monotonic()
        try:
            result = self._c.chat_with_tools(message, **dict(kwargs, tool_executor=traced))
        except Exception as exc:
            self._close_call(call_id, node, symbol, started, None, f"{type(exc).__name__}: {exc}")
            raise
        self._close_call(call_id, node, symbol, started, result, None)
        return result


# ============================ 运行观测执行器 ============================

class RunObserver:
    """执行一次运行并把过程转成事件：全链交给 flow，节点级细节交给 TracingModel。"""

    def __init__(self, run: Run) -> None:
        self.run = run
        self.options = run.options
        self.ledger = ResearchLedger(config.RESEARCH_LEDGER_PATH)
        self.store = RawPayloadStore(config.RESEARCH_RAW_DIR)
        self.gateway = DataGateway(self.ledger, self.store)
        self.reader = LedgerReader(config.RESEARCH_LEDGER_PATH, config.RESEARCH_RAW_DIR)
        self.snapshot = None
        self.packet_ids: list[str] = []

    def close(self) -> None:
        """账本与只读视图都是按查询开/关连接，只有正文库需要显式收尾。"""
        self.store.close()

    def execute(self) -> None:
        run = self.run
        try:
            run.check_cancel()
            client = LLMClient(provider=self.options.provider, model=self.options.model)
            run.log(f"模型：{client.provider}/{client.model}｜标的池：{', '.join(self.options.pool)}｜"
                    f"{RUN_MODES[self.options.mode]}", provider=client.provider, model=client.model)
            if self.options.mode == "snapshot":
                self._run_snapshot(client)
            else:
                self._run_flow(client)
            run.finish("done")
        except RunCancelled:
            run.log("用户停止：已在节点边界安全退出", level="warn")
            run.finish("cancelled", "用户停止")
        except Exception as exc:  # 观测工具不能把异常吞掉：前端要看到失败原因
            run.log(f"运行失败：{type(exc).__name__}: {exc}", level="error")
            run.finish("failed", f"{type(exc).__name__}: {exc}")
        finally:
            self.close()

    # ---------- collect / decide：两种联网节奏都交给 flow ----------

    def _run_flow(self, client: LLMClient) -> None:
        """collect / decide 两个节奏都交给 flow：差别只在是否联网采集与用哪种冻结口径。"""
        run, opts = self.run, self.options
        collecting = opts.mode == "collect"
        run.stage_start("collect", via_flow=True, handled_by="agents/research/flow.py",
                        as_of=opts.as_of or ("实盘：采集完成后由 flow 定时点" if collecting
                                             else "决策：不联网，as_of 默认取当前 UTC"))
        for stage in ("ingest", "freeze", "research", "committee", "portfolio"):
            run.stage_start(stage, via_flow=True)  # 先点亮，产出与耗时在 flow 返回后按真实时刻回填
        started = utc_now()
        model = TracingModel(run, client) if opts.run_research else None
        result = run_research_round(
            FlowOptions(pool=opts.pool, mode=opts.mode, as_of=opts.flow_as_of(),
                        news_days=opts.days, market_lookback=opts.market_lookback, filings=opts.filings,
                        account_state=run.account_state, trace_prefix=run.run_id),
            model=model, ledger=self.ledger, raw_store=self.store)
        self._publish(result, started, utc_now(), collecting=collecting)

    def _publish(self, result, started: datetime, ended: datetime, *, collecting: bool = True) -> None:
        """把 RoundResult 摊成阶段事件：产出是权威数字，耗时按真实时刻切分。"""
        run = self.run
        first_model = run.mark_time("first_model") or ended
        committee = run.mark_time("committee")
        if collecting:
            run.stage_done("collect", {"via_flow": True, "as_of": result.as_of.isoformat(),
                                       "note": "采集与建卡由 flow 一次完成，逐条正文见 Snapshot 详情"},
                           duration_ms=_ms_between(started, first_model))
            run.stage_done("ingest", {"via_flow": True, "cards": result.ingested}, duration_ms=None)
        else:
            # decide 不联网：卡池由每日 daily 积累，本轮只按 available_from 窗口从池里选卡冻结
            run.stage_skip("collect", "决策节奏不联网：卡片由每日 daily 预先入池")
            run.stage_done("ingest", {"via_flow": True, "cards": 0,
                                      "note": "decide 不新建卡，按 available_from 窗口从池选卡冻结"},
                           duration_ms=None)
        self.snapshot = result.snapshot
        if result.snapshot is None:
            for stage in ("freeze", "research", "committee", "portfolio"):
                run.stage_skip(stage, "本轮未冻结 Snapshot")
            return
        run.object("snapshot", result.snapshot.snapshot_id, result.snapshot.model_dump(mode="json"))
        self._publish_evidence(result.snapshot)
        run.stage_done("freeze", {"via_flow": True, "snapshot_id": result.snapshot.snapshot_id,
                                  "as_of": result.snapshot.as_of.isoformat(),
                                  "symbols": list(result.snapshot.symbols),
                                  "evidence": len(result.snapshot.evidence_ids)},
                       duration_ms=None)  # 与采集同属 flow 的一次调用，拆不开就不假分段
        if not result.packets:
            reason = "数据验收轮：只跑采集→建卡→冻结，未注入模型（研究图、委员会与组合政策都跳过）"
            run.stage_skip("research", reason)
            run.stage_skip("committee", reason)
            run.stage_skip("portfolio", reason)
            return
        claims_total = 0
        for packet in result.packets:
            self.packet_ids.append(packet.packet_id)
            run.object("research_packet", packet.packet_id, packet.model_dump(mode="json"),
                       symbol=packet.symbol, trace_id=packet.trace_id)
            for claim in self._claims_of(packet):
                claims_total += 1
                run.object("claim_card", claim.claim_id, claim.model_dump(mode="json"),
                           symbol=claim.symbol, agent=claim.agent, trace_id=claim.trace_id)
        run.stage_done("research", {"packets": len(result.packets), "claims": claims_total,
                                    "packet_ids": list(self.packet_ids)},
                       duration_ms=_ms_between(first_model, committee or ended))
        if result.intent is None:
            run.stage_skip("committee", "flow 本轮未跑委员会")
            run.stage_skip("portfolio", "没有 PortfolioIntent，组合政策无从执行")
            return
        book = self.reader.thesis_book_for_trace(result.intent.trace_id)
        if book is not None:
            run.object("thesis_book", book["thesis_book_id"], book, trace_id=result.intent.trace_id)
        run.object("portfolio_intent", result.intent.intent_id, result.intent.model_dump(mode="json"),
                   trace_id=result.intent.trace_id)
        run.stage_done("committee", {"via_flow": True, "intent_id": result.intent.intent_id,
                                     "thesis_book_id": (book or {}).get("thesis_book_id"),
                                     "items": [{"symbol": i.symbol, "action": i.action, "strength": i.strength,
                                                "priority": i.priority} for i in result.intent.items],
                                     "trace_id": result.intent.trace_id},
                       duration_ms=_ms_between(committee, ended))
        if result.portfolio is None:
            run.stage_skip("portfolio", "flow 本轮未跑组合政策")
            return
        # 组合政策是确定性纯计算，与委员会同属 flow 的一次调用，拆不开就不假分段
        run.stage_start("portfolio", intent_id=result.intent.intent_id)
        p = result.portfolio
        run.object("intent_constraints", p.constraints.intent_id,
                   p.constraints.model_dump(mode="json"), trace_id=p.constraints.trace_id)
        run.object("target_portfolio", p.target.target_portfolio_id,
                   p.target.model_dump(mode="json"), trace_id=p.target.trace_id)
        run.object("risk_projected_portfolio", p.projected.projected_portfolio_id,
                   p.projected.model_dump(mode="json"), trace_id=p.projected.trace_id)
        run.object("order_plan", p.order_plan.order_plan_id,
                   p.order_plan.model_dump(mode="json"), trace_id=p.order_plan.trace_id)
        run.stage_done("portfolio", {"via_flow": True, "order_plan_id": p.order_plan.order_plan_id,
                                     "orders": len(p.order_plan.orders),
                                     "deferred": len(p.order_plan.deferred_trades),
                                     "exposure": round(p.projected.total_exposure, 4),
                                     "adjustments": len(p.projected.adjustments),
                                     "trace_id": p.order_plan.trace_id},
                       duration_ms=None)

    def _publish_evidence(self, snapshot) -> None:
        """外发本轮冻结卡集的抽样：契约对象原样发出，全文另有正文库接口。"""
        run = self.run
        ids = list(snapshot.evidence_ids)[:_MAX_CARD_EVENTS]
        for evidence_id in ids:
            card = self.ledger.get_evidence(evidence_id)
            # 卡片子类别（news/filing/market）已在 data.kind 里，顶层 kind 是对象类别 evidence_card，
            # 不能再传 kind= 否则与 object() 的位置参数撞名。
            run.object("evidence_card", card.evidence_id, card.model_dump(mode="json"),
                       symbol=card.symbol)
        if len(snapshot.evidence_ids) > len(ids):
            run.log(f"卡片逐张外发上限 {_MAX_CARD_EVENTS}，共 {len(snapshot.evidence_ids)} 张；"
                    "完整名单见 Snapshot 详情", level="muted")

    def _claims_of(self, packet):
        for claim_id in packet.claim_card_ids:
            try:
                yield self.ledger.get_claim(claim_id)
            except Exception:  # 账本里查不到的卡说明链路断了，报出来比跳过更有用
                self.run.log(f"ClaimCard {claim_id} 不在账本，跳过逐张外发", level="error")

    # ---------- 单阶段调试：flow 没有的节奏 ----------

    def _run_snapshot(self, client: LLMClient) -> None:
        """复用已冻结 Snapshot 只重跑研究图/委员会：不采集、不冻结，反复调研究节点最省配额。"""
        run, opts = self.run, self.options
        for stage in ("collect", "ingest", "freeze"):
            run.stage_skip(stage, "复用已冻结 Snapshot（flow 无此节奏，观测层直接调用研究原语）")
        run.stage_skip("portfolio", "单阶段调试不经过 flow：组合政策需要本轮账户行情与意图输入，不在此节奏重建")
        self.snapshot = self.ledger.get_snapshot(str(opts.snapshot_id))
        run.object("snapshot", self.snapshot.snapshot_id, self.snapshot.model_dump(mode="json"))
        run.log(f"复用 Snapshot {self.snapshot.snapshot_id}｜标的 {', '.join(self.snapshot.symbols)}"
                f"｜证据 {len(self.snapshot.evidence_ids)} 张｜as_of {self.snapshot.as_of}")
        symbols = [s for s in opts.pool if s in self.snapshot.symbols]
        if not symbols:
            raise ValueError(f"勾选的标的都不在该 Snapshot 内：{self.snapshot.symbols}")

        started = utc_now()
        run.mark("first_model")
        run.stage_start("research", snapshot_id=self.snapshot.snapshot_id, symbols=symbols,
                        topology="event → (fundamental ‖ market) → critic → packet")
        claims_total = 0
        for index, symbol in enumerate(symbols, 1):
            run.check_cancel()
            trace_id = f"{run.run_id}-research-{symbol}"
            graph = PerAssetResearchGraph(ledger=self.ledger, gateway=self.gateway,
                                          model=TracingModel(run, client, symbol=symbol))
            run.log(f"[{index}/{len(symbols)}] 研究图启动：{symbol}", symbol=symbol, trace_id=trace_id)
            packet = graph.run(snapshot_id=self.snapshot.snapshot_id, symbol=symbol,
                               as_of=self.snapshot.as_of, trace_id=trace_id)
            self.packet_ids.append(packet.packet_id)
            run.object("research_packet", packet.packet_id, packet.model_dump(mode="json"),
                       symbol=symbol, trace_id=trace_id)
            for claim in self._claims_of(packet):
                claims_total += 1
                run.object("claim_card", claim.claim_id, claim.model_dump(mode="json"),
                           symbol=claim.symbol, agent=claim.agent, trace_id=trace_id)
            run.log(f"[{symbol}] Packet 收口：claims={len(packet.claim_card_ids)} "
                    f"coverage={packet.citation_coverage:.0%} critic={packet.critic.verdict}",
                    symbol=symbol, level="ok")
        run.stage_done("research", {"packets": len(self.packet_ids), "claims": claims_total,
                                    "packet_ids": list(self.packet_ids)},
                       duration_ms=_ms_between(started, utc_now()))

        if not opts.run_committee:
            run.stage_skip("committee", "单阶段调试：只看研究图（跑全程按钮会连带委员会一起跑）")
            return
        committee_started = utc_now()
        run.mark("committee")
        trace_id = f"{run.run_id}-committee"
        run.stage_start("committee", snapshot_id=self.snapshot.snapshot_id,
                        packets=len(self.packet_ids), trace_id=trace_id)
        committee = Committee(ledger=self.ledger, gateway=self.gateway,
                              model=TracingModel(run, client, symbol=",".join(symbols)))
        intent = committee.run(snapshot_id=self.snapshot.snapshot_id, packet_ids=tuple(self.packet_ids),
                               as_of=self.snapshot.as_of, trace_id=trace_id)
        book = self.reader.thesis_book_for_trace(trace_id)
        if book is not None:
            run.object("thesis_book", book["thesis_book_id"], book, trace_id=trace_id)
        run.object("portfolio_intent", intent.intent_id, intent.model_dump(mode="json"), trace_id=trace_id)
        run.stage_done("committee", {"intent_id": intent.intent_id,
                                     "thesis_book_id": (book or {}).get("thesis_book_id"),
                                     "items": [{"symbol": i.symbol, "action": i.action, "strength": i.strength,
                                                "priority": i.priority} for i in intent.items],
                                     "trace_id": trace_id},
                       duration_ms=_ms_between(committee_started, utc_now()))


# ============================ 进程内状态 ============================

def build_run(payload: dict[str, Any], runs_dir: Path) -> Run:
    """校验前端载荷并创建 Run；账户状态非法时直接报错，不静默用默认值兜底。"""
    options = RunOptions.from_payload(payload)
    account = payload.get("account_state")
    if isinstance(account, str):
        account = json.loads(account or "null") if account.strip() else None
    if not isinstance(account, dict) or not account:
        raise ValueError("账户状态必须是非空 JSON 对象")
    run_id = f"viz_{utc_now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    return Run(run_id, options, runs_dir, account)


def start_run_thread(run: Run) -> threading.Thread:
    thread = threading.Thread(target=RunObserver(run).execute, name=f"viz-{run.run_id}", daemon=True)
    thread.start()
    return thread


class _State:
    """进程内运行登记表：同一时刻只允许一次运行，避免两个 run 并发抢写同一账本。"""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir
        self.reader = LedgerReader(config.RESEARCH_LEDGER_PATH, config.RESEARCH_RAW_DIR)
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}

    def active(self) -> Run | None:
        with self._lock:
            return next((run for run in self._runs.values() if run.state == "running"), None)

    def create(self, payload: dict[str, Any]) -> Run:
        with self._lock:
            run = build_run(payload, self.runs_dir)
            self._runs[run.run_id] = run
            return run

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)


STATE: _State  # 由 main() 注入，handler 通过模块全局访问


def _meta() -> dict[str, Any]:
    return {
        "pool": DEFAULT_POOL,
        "providers": {name: {"models": cfg["models"], "default_model": cfg["default_model"]}
                      for name, cfg in _DEFAULTS.items()},
        "run_modes": [{"value": key, "label": label} for key, label in RUN_MODES.items()],
        "stages": [{"value": key, "label": label} for key, label in STAGE_LABELS.items()],
        "default_account": DEFAULT_ACCOUNT,
        "defaults": {"provider": config.DEFAULT_LLM_PROVIDER, "model": config.DEFAULT_LLM_MODEL or None},
        "versions": {"news": news_collector.NEWS_VERSION, "market": INDICATORS_VERSION,
                     "filings": filings_collector.FILINGS_VERSION},
        "paths": {"ledger": str(config.RESEARCH_LEDGER_PATH), "raw": str(config.RESEARCH_RAW_DIR),
                  "runs": str(STATE.runs_dir), "llm_log": str(config.LOG_DIR / "llm_calls.jsonl")},
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _run_replay(run_id: str) -> dict[str, Any] | None:
    """已结束的运行优先用内存对象；服务重启后回落到 JSONL 文件。"""
    run = STATE.get(run_id)
    if run is not None:
        return {"run_id": run_id, "state": run.state, "error": run.error,
                "options": run.options.__dict__ | {"pool": list(run.options.pool)},
                "account_state": run.account_state, "created_at": run.created_at.isoformat(),
                "events": list(run.events)}
    path = STATE.runs_dir / f"{run_id}.jsonl"
    if not path.exists():
        return None
    meta, events = load_run_events(path)
    end = next((e for e in reversed(events) if e["type"] == "run_end"), {})
    return {"run_id": run_id, "state": end.get("state", "unknown"), "error": end.get("error"),
            "options": meta.get("options"), "account_state": meta.get("account_state"),
            "created_at": meta.get("created_at"), "events": events}


# ============================ HTTP ============================

class Handler(BaseHTTPRequestHandler):
    server_version = "TraderViz/0.1"
    protocol_version = "HTTP/1.1"

    # ---------- 基础响应 ----------

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求体不是合法 JSON") from None
        if not isinstance(parsed, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return parsed

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003 — 覆写标准库签名
        sys.stderr.write("[viz] %s\n" % (format % args))

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 约定
        parsed = urlparse(self.path)
        route = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if route in {"/", "/index.html"}:
                self._static("index.html")
            elif route.startswith("/static/"):
                self._static(route[len("/static/"):])
            elif route == "/api/meta":
                self._json(_meta())
            elif route == "/api/runs":
                self._json({"runs": STATE.reader.run_index(STATE.runs_dir, int(query.get("limit", 50)))})
            elif route == "/api/run":
                replay = _run_replay(query.get("run_id", ""))
                self._json(replay) if replay else self._error(404, "run not found")
            elif route == "/api/stream":
                self._stream(query.get("run_id", ""), int(query.get("after", 0)))
            elif route == "/api/snapshots":
                self._json({"snapshots": STATE.reader.snapshots(int(query.get("limit", 30)))})
            elif route == "/api/snapshot":
                detail = STATE.reader.snapshot_detail(query.get("snapshot_id", ""))
                self._json(detail) if detail else self._error(404, "snapshot not found")
            elif route == "/api/traces":
                self._json({"traces": STATE.reader.traces(int(query.get("limit", 40)))})
            elif route == "/api/trace":
                self._json(STATE.reader.trace_detail(query.get("trace_id", "")))
            elif route == "/api/object":
                detail = STATE.reader.object_detail(query.get("kind", ""), query.get("id", ""))
                self._json(detail) if detail else self._error(404, "object not found")
            elif route == "/api/raw":
                self._raw(query.get("evidence_id", ""))
            else:
                self._error(404, f"unknown route: {route}")
        except ValueError as exc:
            self._error(400, str(exc))
        except BrokenPipeError:
            pass  # 浏览器断开 SSE 属于常态，不该污染服务端日志
        except Exception as exc:  # 观测服务不能因单次查询崩溃
            self._error(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/run":
                self._start_run(self._body())
            elif parsed.path == "/api/run/cancel":
                body = self._body()
                run = STATE.get(str(body.get("run_id", "")))
                if run is None:
                    self._error(404, "run not found")
                else:
                    run.cancel()
                    self._json({"run_id": run.run_id, "cancelling": True})
            else:
                self._error(404, f"unknown route: {parsed.path}")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:
            self._error(500, f"{type(exc).__name__}: {exc}")

    # ---------- 处理函数 ----------

    def _static(self, relative: str) -> None:
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():
            self._error(404, f"static not found: {relative}")
            return
        self._send(200, target.read_bytes(), _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

    def _start_run(self, payload: dict[str, Any]) -> None:
        active = STATE.active()
        if active is not None:
            self._json({"error": "已有运行在进行中", "run_id": active.run_id, "state": active.state}, 409)
            return
        run = STATE.create(payload)
        start_run_thread(run)
        self._json({"run_id": run.run_id, "state": run.state}, 202)

    def _raw(self, evidence_id: str) -> None:
        detail = STATE.reader.object_detail("evidence_card", evidence_id)
        if detail is None:
            self._error(404, "evidence card not found")
            return
        self._json({"evidence_id": evidence_id, "card": detail["data"],
                    "payload": STATE.reader.raw_payload(detail["data"])})

    def _stream(self, run_id: str, after: int) -> None:
        """SSE：把事件逐条推给前端；无事件时挂住连接，而不是让前端轮询。

        响应不设 Content-Length、也不分块，而是声明 `Connection: close` 并在推完
        后关连接：基类会在下一次循环里重新解析请求，若沿用 keep-alive 就会与
        浏览器的事件流语义打架。
        """
        run = STATE.get(run_id)
        if run is None:
            if (STATE.runs_dir / f"{run_id}.jsonl").exists():
                self._json({"error": "该运行已结束且不在本进程内，请用 /api/run 取完整事件"}, 410)
            else:
                self._error(404, "run not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        cursor = after
        try:
            while True:
                for event in run.wait(cursor, timeout=15.0):
                    self._sse(event)
                    cursor = event["seq"]
                if run.state != "running":
                    for event in run.since(cursor):
                        self._sse(event)
                        cursor = event["seq"]
                    self._sse({"type": "stream_end", "seq": cursor, "state": run.state})
                    return
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return  # 客户端离开是常态：刷新或关闭页面

    def _sse(self, event: dict[str, Any]) -> None:
        self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n".encode("utf-8"))
        self.wfile.flush()


def _enable_utf8_console() -> None:
    """Windows 控制台默认 GBK 码页会把中文提示打成乱码；码页与 stdout 两层一起改才彻底。"""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="研究链路可视化调试台")
    parser.add_argument("--host", default="127.0.0.1", help="只允许本机监听，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR), help="运行事件流的落盘目录")
    return parser.parse_args()


def main() -> int:
    global STATE
    _enable_utf8_console()
    args = _parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"拒绝监听 {args.host}：本工具会暴露账本与正文，只允许本机地址")
        return 2
    runs_dir = Path(args.runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    STATE = _State(runs_dir)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"可视化调试台： http://{args.host}:{args.port}")
    print(f"  账本   : {config.RESEARCH_LEDGER_PATH}")
    print(f"  正文库 : {config.RESEARCH_RAW_DIR}")
    print(f"  运行流 : {runs_dir}/<run_id>.jsonl")
    print("  Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
