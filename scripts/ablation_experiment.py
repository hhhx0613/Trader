"""
Stage 4 消融实验（简化版）

对比：
  1. 无 L1 记忆 vs 有 L1 记忆
  2. 无置信度校准 vs 有置信度校准

实验设计：
  - 固定其他参数，只改变实验变量
  - 记录每次实验的结果（累计收益、最大回撤、夏普比率）
  - 输出对比表
"""

import os
import sys
import pandas as pd
from pathlib import Path

# 项目根目录
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import config
from core.multi_stock_engine import MultiStockBacktestEngine
from agents.decision_func import decide_formula_llm
from agents.stock_selector import DEFAULT_CANDIDATE_POOL
from agents.llm_analyst import LLMAnalystAgent
from agents.confidence_calibrator import get_calibrator
from core.data.news_data import get_news_at_date
from scripts.run_backtest_stage3 import download_data


def run_experiment(
    name: str,
    enable_l1_memory: bool,
    market_data: dict,
    news_df: pd.DataFrame,
) -> dict:
    """
    运行单次实验。
    
    每次调用都会清除 LLM 缓存，确保重新调用 LLM API
    """
    print(f"\n{'='*60}")
    print(f"实验：{name}")
    print(f"  L1 记忆：{'启用' if enable_l1_memory else '禁用'}")
    print(f"{'='*60}")
    
    # 清除 LLM 缓存
    import sqlite3
    from core.data.llm_cache_db import get_cache_db
    cache_db = get_cache_db()
    conn = sqlite3.connect(str(cache_db.db_path))
    conn.execute("DELETE FROM llm_analysis_cache")
    conn.commit()
    conn.close()
    print(f"  [已清除 LLM 缓存]")
    
    # 创建决策函数（只控制 enable_l1_memory）
    def decision_wrapper(date, market_state, candidate_pool, news_df, current_holdings):
        # 临时修改 stock_selector 的行为
        from agents import stock_selector
        original_analyze = stock_selector.analyze_candidate_pool
        
        def patched_analyze(date, candidate_pool, news_df, provider=None, model=None, enable_calibration=False):
            if provider is None:
                provider = config.DEFAULT_LLM_PROVIDER
            if model is None:
                model = config.DEFAULT_LLM_MODEL
            
            # 创建 agent（控制 L1 记忆）
            agent = LLMAnalystAgent(provider=provider, model=model, enable_l1_memory=enable_l1_memory)
            results = []
            
            print(f"\n[选股分析] {date}: 分析 {len(candidate_pool)} 只候选股...")
            
            for symbol in candidate_pool:
                try:
                    news_list = get_news_at_date(news_df, symbol, date) if news_df is not None else []
                    
                    if not news_list:
                        results.append({
                            "symbol": symbol,
                            "direction": "neutral",
                            "confidence": 0.0,
                            "analysis": {"direction": "neutral", "confidence": 0.0},
                        })
                        continue
                    
                    analysis = agent.analyze(symbol, date, news_list)
                    direction = analysis.get("direction", "neutral")
                    confidence = float(analysis.get("confidence", 0.0))
                    
                    print(f"  {symbol}: {direction} (conf={confidence:.2f}, news={len(news_list)}条)")
                    
                    results.append({
                        "symbol": symbol,
                        "direction": direction,
                        "confidence": confidence,
                        "analysis": analysis,
                    })
                except Exception as e:
                    print(f"  [WARN] {symbol} 分析失败：{e}")
                    continue
            
            results.sort(key=lambda x: x["confidence"], reverse=True)
            return results
        
        # 临时替换
        stock_selector.analyze_candidate_pool = patched_analyze
        try:
            result = decide_formula_llm(date, market_state, candidate_pool, news_df, current_holdings)
        finally:
            stock_selector.analyze_candidate_pool = original_analyze
        return result
    
    # 创建引擎
    engine = MultiStockBacktestEngine(decide_func=decision_wrapper)
    recorder = engine.run(market_data, news_df, DEFAULT_CANDIDATE_POOL)
    
    # 提取结果
    equity_curve = recorder.equity_curve
    if len(equity_curve) > 0:
        final_equity = equity_curve[-1][1]
        total_return = (final_equity / 100000 - 1) * 100
        trades = recorder.trades
        trade_count = len(trades)
        
        # 计算最大回撤
        equities = [e[1] for e in equity_curve]
        equity_series = pd.Series(equities)
        running_max = equity_series.cummax()
        drawdown = (equity_series - running_max) / running_max
        max_dd = drawdown.min() * 100
        
        # 计算夏普比率
        daily_returns = equity_series.pct_change().dropna()
        if len(daily_returns) > 0 and daily_returns.std() > 0:
            sharpe = (daily_returns.mean() / daily_returns.std()) * (252 ** 0.5)
        else:
            sharpe = 0.0
        
        return {
            "name": name,
            "return": total_return,
            "max_dd": max_dd,
            "sharpe": sharpe,
            "trades": trade_count,
        }
    else:
        return {
            "name": name,
            "return": 0.0,
            "max_dd": 0.0,
            "sharpe": 0.0,
            "trades": 0,
        }


def main():
    print("=" * 60)
    print("  Stage 4 消融实验")
    print("=" * 60)
    
    # 先加载数据
    print("\n  [1/5] 加载数据...")
    market_data, news_df = download_data(
        DEFAULT_CANDIDATE_POOL,
        start_date=config.DEFAULT_START_DATE,
        end_date=config.DEFAULT_END_DATE,
    )
    
    results = []
        
    # 实验 1：无 L1 记忆
    results.append(run_experiment(
        "无记忆",
        enable_l1_memory=False,
        market_data=market_data,
        news_df=news_df,
    ))
        
    # 实验 2：有 L1 记忆
    results.append(run_experiment(
        "有记忆 + 无校准",
        enable_l1_memory=True,
        market_data=market_data,
        news_df=news_df,
    ))
    
    # 输出对比表
    print("\n" + "=" * 60)
    print("  消融实验结果对比")
    print("=" * 60)
    df = pd.DataFrame(results)
    print(df.to_string(index=False))
    
    # 保存结果
    output_dir = Path(config.OUTPUT_DIR) / "ablation"
    output_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_dir / "ablation_results.csv", index=False)
    print(f"\n结果已保存到 {output_dir / 'ablation_results.csv'}")


if __name__ == "__main__":
    main()
