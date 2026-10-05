"""模型可自主发起的只读补查工具目录与受控执行循环（唯一实现处）。

设计边界：默认的本周批次证据由各 Agent 的注入 hook 直接调用 Gateway 拼进初始 prompt，
不经本模块；本文件只承载**暴露给模型 function calling** 的工具——目前唯一保留的是
`search_evidence`（同维度定向补查：每个节点只能搜自己那一类证据，维度分工不给侧门）。
类别取数（get_news_batch 等）不再是模型工具，所以不在此登记，避免留下无人挂载的死 schema。

`ResearchTools` 一遍实现预算、参数校验、正文截断与 trace 记账；Agent 文件只声明
挂载哪几个工具与本节点可见的证据类别，不重复实现这套控制循环。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any

from core.research import DataGateway

from .types import ResearchState


_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "search_evidence": {"type": "object", "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 200},
        "kinds": {"type": "array", "items": {"type": "string", "enum": ["news", "filing", "market"]}, "maxItems": 3},
    }, "required": ["query"], "additionalProperties": False},
}


_ALL_KINDS = ("news", "filing", "market")


class ResearchTools:
    """将单节点模型补查限制在同一冻结 Snapshot、本节点证据类别白名单内的只读查询。"""

    def __init__(self, *, gateway: DataGateway, state: ResearchState, node: str, allowed_tools: tuple[str, ...],
                 max_calls: int, max_characters: int = 24_000,
                 allowed_kinds: tuple[str, ...] = _ALL_KINDS) -> None:
        if not set(allowed_tools) <= set(_TOOL_SCHEMAS):
            raise ValueError("unknown research tool in allowlist")
        if not allowed_kinds or not set(allowed_kinds) <= set(_ALL_KINDS):
            raise ValueError("allowed_kinds must be a non-empty subset of news/filing/market")
        self.gateway = gateway
        self.state = state
        self.node = node
        self.allowed_tools = allowed_tools
        self.max_calls = max_calls
        self.max_characters = max_characters
        self.allowed_kinds = tuple(allowed_kinds)
        self.calls: list[dict[str, Any]] = []
        self.evidence_ids: set[str] = set()
        self._used_characters = 0

    @property
    def tools(self) -> list[dict[str, Any]]:
        """schema 里的 kinds enum 收窄到本节点维度：模型从一开始就看不到越界选项。"""
        exposed = []
        for name in self.allowed_tools:
            schema = _TOOL_SCHEMAS[name]
            if name == "search_evidence" and self.allowed_kinds != _ALL_KINDS:
                schema = copy.deepcopy(schema)
                schema["properties"]["kinds"]["items"]["enum"] = list(self.allowed_kinds)
            exposed.append({"type": "function", "function": {"name": name,
                          "description": "Read PIT-valid records frozen in the current research Snapshot; never fetches from the network.",
                          "parameters": schema}})
        return exposed

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """供模型客户端回调；不接收 snapshot_id/symbol/as_of，防止边界被提示注入覆盖。"""
        if name not in self.allowed_tools:
            raise ValueError(f"tool is not allowed for {self.node}: {name}")
        if len(self.calls) >= self.max_calls:
            raise ValueError(f"tool call budget exceeded for {self.node}")
        self._validate_arguments(name, arguments)
        result = self._dispatch(name, arguments)
        bounded = self._bound_result(result)
        evidence_ids = self._collect_evidence_ids(bounded)
        self.evidence_ids.update(evidence_ids)
        count = self._result_count(bounded)
        call = {"tool": name, "arguments": arguments, "result_count": count,
                "evidence_ids": sorted(evidence_ids)}
        self.calls.append(call)
        self.gateway.ledger.record_tool_call(trace_id=self.state["trace_id"], snapshot_id=self.state["snapshot_id"],
                                              symbol=self.state["symbol"], node=self.node, tool_name=name,
                                              arguments=arguments, result_count=call["result_count"],
                                              evidence_ids=call["evidence_ids"], requested_at=datetime.now().astimezone())
        if count == 0:
            # 空结果必须自解释：裸 [] 会被模型误读成“参数猜错了”，转而枚举式换词重试。
            return json.dumps({"records": [], "note": self._empty_result_note()}, ensure_ascii=False)
        return json.dumps(bounded, ensure_ascii=False, default=str, separators=(",", ":"))

    def _empty_result_note(self) -> str:
        """把“为什么空”回传给模型，从根上阻断无意义的并行枚举。"""
        return ("本次补查在冻结证据里没有命中；请基于初始 prompt 已注入的批次作答，"
                "不要用近似关键词反复重试；证据仍不足则显式返回 data_request 或弃权。")

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        common = {"snapshot_id": self.state["snapshot_id"], "symbol": self.state["symbol"], "as_of": self.state["as_of"]}
        if name == "search_evidence":
            # 不传 kinds 时的默认必须是本节点维度，而不是 Gateway 的全部三类，否则白名单会被默认值绕过。
            # 审计仍记模型原始入参，默认化只发生在执行层。
            args = dict(arguments)
            args.setdefault("kinds", list(self.allowed_kinds))
            return self.gateway.search_evidence(**common, **args)
        raise AssertionError(name)

    def _validate_arguments(self, name: str, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict) or set(arguments) - set(_TOOL_SCHEMAS[name]["properties"]):
            raise ValueError("tool arguments do not match allowlisted schema")
        if name == "search_evidence":
            if not isinstance(arguments.get("query"), str) or not arguments["query"].strip():
                raise ValueError("search_evidence requires a non-empty query")
            if len(arguments["query"]) > 200 or ("kinds" in arguments and (not isinstance(arguments["kinds"], list)
                    or len(arguments["kinds"]) > 3
                    or not set(arguments["kinds"]) <= set(self.allowed_kinds))):
                raise ValueError("search_evidence arguments are outside the tool schema or this node's evidence scope")

    def _bound_result(self, value: Any) -> Any:
        """在总预算内截断正文，绝不删除 EvidenceCard ID。"""
        if isinstance(value, list):
            return [self._bound_item(item) for item in value]
        if isinstance(value, dict):
            return {key: [self._bound_item(item) for item in items] if isinstance(items, list) else items
                    for key, items in value.items()}
        return value

    def _bound_item(self, item: Any) -> Any:
        if not isinstance(item, dict) or "text" not in item:
            return item
        copy = dict(item)
        remaining = max(0, self.max_characters - self._used_characters)
        copy["text"] = str(copy["text"])[:remaining]
        self._used_characters += len(copy["text"])
        return copy

    @staticmethod
    def _collect_evidence_ids(value: Any) -> set[str]:
        if isinstance(value, list):
            return {str(item["evidence_id"]) for item in value if isinstance(item, dict) and "evidence_id" in item}
        if isinstance(value, dict):
            return {str(item["evidence_id"]) for items in value.values() if isinstance(items, list)
                    for item in items if isinstance(item, dict) and "evidence_id" in item}
        return set()

    @staticmethod
    def _result_count(value: Any) -> int:
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            return sum(len(items) for items in value.values() if isinstance(items, list))
        return int(value is not None)
