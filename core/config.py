"""
配置转发垫片 —— 实际配置在项目根目录的 config.py 中。

此文件保留仅为向后兼容，core 内部模块的 `from . import config` 仍可使用。
新代码请直接 `import config`。
"""

from config import *  # noqa: F401,F403 — 转发根 config 的全部属性
