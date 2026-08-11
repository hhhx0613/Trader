"""
置信度校准模块（Confidence Calibrator）

设计说明：
  - LLM 自报的 confidence 可能不准（如自报 0.8，实际胜率只有 50%）
  - 用历史数据建立映射：(LLM confidence → 实际胜率)
  - 校准后用实际胜率替代 LLM 自报置信度

校准流程：
  1. 收集历史数据：(symbol, date, direction, confidence, actual_outcome)
  2. 分箱统计：将 confidence 分成 [0, 0.2), [0.2, 0.4), ..., [0.8, 1.0]
  3. 计算每箱的实际胜率
  4. 建立校准映射表

使用方式：
  calibrator = ConfidenceCalibrator()
  calibrator.fit(historical_results)  # 用历史数据训练
  calibrated_conf = calibrator.calibrate(raw_confidence)
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

from core import config


class ConfidenceCalibrator:
    """
    置信度校准器
    
    将 LLM 自报的 confidence 校准为实际胜率。
    """
    
    # 分箱边界
    BIN_EDGES = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    BIN_CENTERS = [0.1, 0.3, 0.5, 0.7, 0.9]  # 每箱中心点
    
    def __init__(self, calibration_path: Optional[Path] = None):
        """
        参数：
          calibration_path: 校准表存储路径（默认 data/cache/llm/calibration.json）
        """
        if calibration_path is None:
            self.calibration_path = Path(config.CACHE_DIR) / "llm" / "calibration.json"
        else:
            self.calibration_path = Path(calibration_path)
        
        # 校准映射表：bin_center -> (win_rate, sample_count)
        self.calibration_map: Dict[float, Tuple[float, int]] = {}
        
        # 尝试加载已有校准表
        self._load_calibration()
    
    def fit(self, results: List[Dict]) -> None:
        """
        用历史结果训练校准表。
        
        参数：
          results: 历史分析结果列表，每项包含：
            - direction: "bullish" / "bearish" / "neutral"
            - confidence: LLM 自报置信度 (0.0-1.0)
            - actual_return: 实际收益率（正=盈利，负=亏损）
        """
        # 分箱统计
        bin_stats = defaultdict(lambda: {"wins": 0, "total": 0})
        
        for r in results:
            conf = r.get("confidence", 0.5)
            direction = r.get("direction", "neutral")
            actual_return = r.get("actual_return", 0.0)
            
            # 只统计有明确方向的（neutral 不参与校准）
            if direction == "neutral":
                continue
            
            # 判断是否"正确"：bullish 且实际涨，或 bearish 且实际跌
            is_correct = (
                (direction == "bullish" and actual_return > 0) or
                (direction == "bearish" and actual_return < 0)
            )
            
            # 找到对应的箱
            bin_idx = self._get_bin_index(conf)
            if bin_idx >= 0:
                bin_stats[bin_idx]["total"] += 1
                if is_correct:
                    bin_stats[bin_idx]["wins"] += 1
        
        # 计算每箱的胜率
        self.calibration_map = {}
        for bin_idx, stats in bin_stats.items():
            if stats["total"] >= 3:  # 至少 3 个样本才校准
                win_rate = stats["wins"] / stats["total"]
                self.calibration_map[self.BIN_CENTERS[bin_idx]] = (win_rate, stats["total"])
        
        # 保存校准表
        self._save_calibration()
        
        print(f"[Calibrator] 校准完成，{len(self.calibration_map)} 个箱有数据")
        for center, (win_rate, count) in sorted(self.calibration_map.items()):
            print(f"  confidence [{center-0.1:.1f}, {center+0.1:.1f}): 胜率={win_rate:.2%} (n={count})")
    
    def calibrate(self, confidence: float) -> float:
        """
        校准置信度。
        
        参数：
          confidence: LLM 自报置信度 (0.0-1.0)
        
        返回：
          校准后的置信度（实际胜率）。如果该箱无数据，返回原始值。
        """
        if not self.calibration_map:
            return confidence
        
        bin_idx = self._get_bin_index(confidence)
        if bin_idx < 0:
            return confidence
        
        bin_center = self.BIN_CENTERS[bin_idx]
        if bin_center in self.calibration_map:
            win_rate, _ = self.calibration_map[bin_center]
            return win_rate
        
        return confidence
    
    def _get_bin_index(self, confidence: float) -> int:
        """返回 confidence 对应的箱索引（0-4），越界返回 -1"""
        for i in range(len(self.BIN_EDGES) - 1):
            if self.BIN_EDGES[i] <= confidence < self.BIN_EDGES[i + 1]:
                return i
        # 特殊情况：confidence == 1.0
        if confidence >= 1.0:
            return len(self.BIN_EDGES) - 2
        return -1
    
    def _save_calibration(self) -> None:
        """保存校准表到 JSON"""
        self.calibration_path.parent.mkdir(parents=True, exist_ok=True)
        data = {str(k): {"win_rate": v[0], "count": v[1]} for k, v in self.calibration_map.items()}
        with open(self.calibration_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def _load_calibration(self) -> None:
        """从 JSON 加载校准表"""
        if not self.calibration_path.exists():
            return
        
        try:
            with open(self.calibration_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.calibration_map = {
                float(k): (v["win_rate"], v["count"])
                for k, v in data.items()
            }
        except Exception as e:
            print(f"[Calibrator] 加载校准表失败：{e}")


# 全局实例（单例模式）
_calibrator_instance: Optional[ConfidenceCalibrator] = None


def get_calibrator() -> ConfidenceCalibrator:
    """获取全局校准器实例"""
    global _calibrator_instance
    if _calibrator_instance is None:
        _calibrator_instance = ConfidenceCalibrator()
    return _calibrator_instance
