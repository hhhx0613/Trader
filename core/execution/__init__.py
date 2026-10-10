"""冻结订单的模拟执行边界；不接收 Agent 或组合模型的原始输出。"""

from .simulated import ExecutionAccount, ExecutionLimits, ExecutionResult, SimulatedExecution

__all__ = ["ExecutionAccount", "ExecutionLimits", "ExecutionResult", "SimulatedExecution"]
