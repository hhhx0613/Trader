"""研究 prompt 的文件化版本管理 / File-backed, versioned research prompts."""

from __future__ import annotations

import hashlib
from pathlib import Path


_ROOT = Path(__file__).parent.parent / "prompts" / "research"


def load_prompt(name: str) -> tuple[str, str]:
    """读取单一职责 prompt，并以内容 hash 作为审计版本。"""
    path = _ROOT / f"{name}.txt"
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"research prompt is empty: {path}")
    return content, hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
