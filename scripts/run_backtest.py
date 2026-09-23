#!/usr/bin/env python3
"""
回测脚本（统一入口）

用法：
  python scripts/run_backtest.py
  python scripts/run_backtest.py --pool AAPL NVDA MSFT --start 2025-08-11 --end 2026-08-07
  python scripts/run_backtest.py --skip-vader
"""

import sys
from pathlib import Path

# 让直接运行（python scripts/xxx.py）也能找到 core 模块
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import argparse
import csv
from datetime import datetime
import hashlib
import json
import subprocess

import pandas as pd

from core.data.market_data import fetch_ohlcv
from core.data.news_data import fetch_news
from core.indicators import compute_all_indicators
from core.multi_stock_engine import MultiStockBacktestEngine
from core.recorder import Recorder
from agents.decision_func import decide_formula_llm, decide_formula_vader
from agents.stock_selector import DEFAULT_CANDIDATE_POOL
from core import config

# 指标预热期（日历天数）：60 天覆盖 RSI/ADX 的预热窗口
INDICATOR_WARMUP_DAYS = 60
_REGISTRY_PATH = Path(_PROJECT_ROOT) / "docs" / "experiment_index.csv"
_REPORT_PATH = Path(_PROJECT_ROOT) / "docs" / "backtest_report.md"

_REGISTRY_COLUMNS = [
    "experiment_id", "status", "purpose", "strategy", "cumulative_return", "sharpe",
    "max_drawdown", "delta_return_vs_baseline", "delta_sharpe_vs_baseline", "change_note",
    "known_limitations", "test_period", "ppo_model_path", "output_dir", "manifest_path",
    "run_id", "created_at", "control_experiment", "baseline_strategy", "annualized_return",
    "annualized_volatility", "trades", "turnover", "annualized_turnover", "total_commission",
    "universe", "selection", "position_weights", "cost_model", "code_revision", "git_dirty",
    "git_diff_name_status", "ppo_model_sha256", "ppo_device", "seed", "market_data_sha256",
    "news_data_sha256",
]


def parse_args():
    parser = argparse.ArgumentParser(description="统一回测脚本")
    parser.add_argument("--pool", nargs="+", default=None,
                        help=f"候选股票池（默认：10 只）: {DEFAULT_CANDIDATE_POOL}")
    parser.add_argument("--start", default=None,
                        help=f"起始日期（默认：{config.DEFAULT_START_DATE}）")
    parser.add_argument("--end", default=None,
                        help=f"结束日期（默认：{config.DEFAULT_END_DATE}）")
    parser.add_argument("--skip-vader", action="store_true",
                        help="跳过 VADER 路径")
    parser.add_argument("--ppo-model", default=None,
                        help="PPO 模型路径（默认 models/ppo_stage1.zip）")
    parser.add_argument("--ppo-device", default="cpu", choices=["cpu", "cuda"],
                        help="PPO 推理设备（默认 cpu）")
    parser.add_argument("--experiment-id", default=None,
                        help="实验标识；默认使用本次回测 run_id")
    parser.add_argument("--purpose", required=True,
                        help="本次实验要验证的假设或目的")
    parser.add_argument("--change-note", required=True,
                        help="相对对照实验的代码、配置或模型改动；无改动时明确填写")
    parser.add_argument("--known-limitations", required=True,
                        help="当前已知问题、局限或未完成验证；无则填写“无”")
    parser.add_argument("--control-experiment", default="",
                        help="对照实验 ID；没有对照时留空")
    parser.add_argument("--baseline-strategy", default="LLM Top-K",
                        help="同次回测用于自动计算差值的基线策略（默认 LLM Top-K）")
    parser.add_argument("--seed", default="",
                        help="模型训练或随机化种子；不适用时填写“不适用”")
    parser.add_argument("--status", default="provisional",
                        choices=["provisional", "valid", "invalid", "retired"],
                        help="实验状态（默认 provisional，未经审阅不能标记 valid）")
    args = parser.parse_args()

    # 默认值
    if args.pool is None:
        args.pool = DEFAULT_CANDIDATE_POOL
    if args.start is None:
        args.start = config.DEFAULT_START_DATE
    if args.end is None:
        args.end = config.DEFAULT_END_DATE

    return args


def download_data(candidate_pool, start_date, end_date):
    """下载行情数据和新闻数据"""
    print("\n" + "=" * 60)
    print("  [1/3] 数据采集")
    print("=" * 60)

    # 预热起始日：多拉 15 天给指标计算用
    warmup_start = (pd.Timestamp(start_date) - pd.Timedelta(days=INDICATOR_WARMUP_DAYS)).strftime('%Y-%m-%d')

    # --- 行情数据 ---
    print(f"\n  下载 {len(candidate_pool)} 只候选股行情（含 {INDICATOR_WARMUP_DAYS} 天预热）...")
    market_data = {}
    for symbol in candidate_pool:
        try:
            df = fetch_ohlcv(symbol=symbol, start_date=warmup_start, end_date=end_date)
            if df is not None and len(df) > 0:
                # 在扩展数据上计算指标（含预热期）
                df = compute_all_indicators(df)
                # 裁回回测区间
                df = df[df.index >= pd.Timestamp(start_date)]
                market_data[symbol] = df
                print(f"    [OK] {symbol}: {len(df)} bars "
                      f"({df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')})")
            else:
                print(f"    [WARN] {symbol}: 无数据")
        except Exception as e:
            print(f"    [ERROR] {symbol}: {e}")

    if not market_data:
        raise RuntimeError("没有成功下载任何行情数据，终止")

    # --- 新闻数据 ---
    print(f"\n  下载候选股新闻...")
    all_news = []
    for symbol in candidate_pool:
        try:
            news_df = fetch_news(symbol=symbol, start_date=start_date, end_date=end_date)
            if news_df is not None and len(news_df) > 0:
                news_df["symbol"] = symbol
                all_news.append(news_df)
                print(f"    [OK] {symbol}: {len(news_df)} 条新闻")
            else:
                print(f"    [WARN] {symbol}: 无新闻")
        except Exception as e:
            print(f"    [WARN] {symbol} 新闻失败：{e}")

    if all_news:
        news_df = pd.concat(all_news, ignore_index=True).sort_values("datetime").reset_index(drop=True)
        print(f"\n  [OK] 总新闻：{len(news_df)} 条")
    else:
        news_df = pd.DataFrame(columns=["datetime", "title", "summary", "source", "sentiment_score", "symbol"])
        print(f"\n  [WARN] 无新闻数据")

    return market_data, news_df


def run_multi_stock_backtest(decide_func, market_data, news_df, candidate_pool, exposure_predictor=None):
    """用指定的决策函数跑多股票回测，返回 recorder。

    exposure_predictor 非空时挂载 PPO 择时：调仓日仅覆盖总暴露，选股/权重不变。
    """
    engine = MultiStockBacktestEngine(decide_func=decide_func, exposure_predictor=exposure_predictor)
    return engine.run(market_data, news_df, candidate_pool)


def create_buy_and_hold_recorder(market_data, initial_capital):
    """构造 Buy & Hold 等权基准：第一天等权买入全池，持有不动。"""
    symbols = list(market_data.keys())
    n = len(symbols)
    per_stock = initial_capital / n

    # 各股首日收盘价买入
    holdings = {}
    for sym in symbols:
        price = market_data[sym].iloc[0]["close"]
        holdings[sym] = per_stock / price

    # 以数据最长的股票为基准日期序列
    ref = max(market_data.values(), key=len)
    dates = ref.index

    recorder = Recorder()
    for date in dates:
        equity = 0.0
        for sym in symbols:
            df = market_data[sym]
            if date in df.index:
                equity += holdings[sym] * df.loc[date, "close"]
            else:
                equity += holdings[sym] * df.iloc[-1]["close"]
        recorder.log_equity(date, equity)

    return recorder


def _git_revision():
    """记录当前代码版本；无 git 环境时明确标记未知而不阻断回测。"""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=_PROJECT_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _git_output(*args):
    """尽力采集 Git 证据；缺失时保留明确标记而不阻断回测。"""
    try:
        return subprocess.run(
            ["git", *args], cwd=_PROJECT_ROOT, capture_output=True,
            text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _sha256_file(path):
    path = Path(path)
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dataframe_sha256(df):
    """记录本次实际输入数据的内容指纹，而非仅记录缓存文件名。"""
    digest = hashlib.sha256()
    digest.update("|".join(map(str, df.columns)).encode("utf-8"))
    digest.update("|".join(map(str, df.dtypes)).encode("utf-8"))
    digest.update(pd.util.hash_pandas_object(df, index=True).values.tobytes())
    return digest.hexdigest()


def _metric_number(value):
    if value is None or value == "N/A":
        return None
    text = str(value).replace("$", "").replace(",", "").strip()
    if text.endswith("%"):
        return float(text[:-1]) / 100
    return float(text)


def _stringify(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "" if value is None else str(value)


def _registry_rows(metadata, metrics_by_strategy, output_dir):
    research = metadata["research"]
    baseline = metrics_by_strategy.get(research["baseline_strategy"])
    baseline_return = _metric_number(baseline.get("累计收益率")) if baseline else None
    baseline_sharpe = _metric_number(baseline.get("夏普比率")) if baseline else None

    rows = []
    for strategy, metrics in metrics_by_strategy.items():
        cumulative_return = _metric_number(metrics.get("累计收益率"))
        sharpe = _metric_number(metrics.get("夏普比率"))
        rows.append({
            "experiment_id": metadata["experiment_id"],
            "run_id": metadata["run_id"],
            "created_at": metadata["created_at"],
            "status": research["status"],
            "purpose": research["purpose"],
            "change_note": research["change_note"],
            "known_limitations": research["known_limitations"],
            "control_experiment": research["control_experiment"],
            "baseline_strategy": research["baseline_strategy"],
            "code_revision": metadata["code_revision"],
            "git_dirty": metadata["code_snapshot"]["git_dirty"],
            "git_diff_name_status": metadata["code_snapshot"]["git_diff_name_status"],
            "test_period": metadata["test_period"],
            "universe": _stringify(metadata["universe"]),
            "selection": metadata["selection"],
            "position_weights": metadata["position_weights"],
            "cost_model": _stringify(metadata["cost_model"]),
            "ppo_model_path": metadata["ppo_model_path"] or "",
            "ppo_model_sha256": metadata["ppo_model_sha256"] or "",
            "ppo_device": metadata["ppo_device"] or "",
            "seed": research["seed"],
            "market_data_sha256": metadata["data_snapshot"]["market_data_sha256"],
            "news_data_sha256": metadata["data_snapshot"]["news_data_sha256"],
            "strategy": strategy,
            "cumulative_return": cumulative_return,
            "max_drawdown": _metric_number(metrics.get("最大回撤")),
            "sharpe": sharpe,
            "annualized_return": _metric_number(metrics.get("年化收益率")),
            "annualized_volatility": _metric_number(metrics.get("年化波动率")),
            "trades": _metric_number(metrics.get("总交易次数")),
            "turnover": _metric_number(metrics.get("换手率")),
            "annualized_turnover": _metric_number(metrics.get("年化换手率")),
            "total_commission": _metric_number(metrics.get("总手续费")),
            "delta_return_vs_baseline": (
                cumulative_return - baseline_return
                if baseline_return is not None and cumulative_return is not None else None
            ),
            "delta_sharpe_vs_baseline": (
                sharpe - baseline_sharpe
                if baseline_sharpe is not None and sharpe is not None else None
            ),
            "output_dir": str(output_dir.relative_to(_PROJECT_ROOT)).replace("\\", "/"),
            "manifest_path": str((output_dir / "manifest.json").relative_to(_PROJECT_ROOT)).replace("\\", "/"),
        })
    return rows


def _render_experiment_report(rows):
    """从结构化登记册重建可读报告，避免手工报告和实验数据漂移。"""
    groups = {}
    for row in rows:
        groups.setdefault(row["experiment_id"], []).append(row)

    lines = [
        "# 回测对比报告",
        "",
        "> 本文件由 `docs/experiment_index.csv` 自动生成，请勿手工编辑。",
        "> `provisional`、`invalid` 和 `retired` 结果仅用于追溯，不得直接作为论文有效结论。",
        "",
        "## 关键对比",
        "",
        "| 实验 | 状态 | 策略 | 累计收益 | 夏普 | 最大回撤 | 相对 LLM 收益 | 相对 LLM 夏普 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    percent = lambda value: "N/A" if value in (None, "") else f"{float(value):.2%}"
    number = lambda value: "N/A" if value in (None, "") else f"{float(value):.2f}"
    for row in sorted(rows, key=lambda item: (item["created_at"], item["experiment_id"], item["strategy"]), reverse=True):
        lines.append(
            f"| {row['experiment_id']} | {row['status']} | {row['strategy']} | "
            f"{percent(row['cumulative_return'])} | {number(row['sharpe'])} | "
            f"{percent(row['max_drawdown'])} | {percent(row['delta_return_vs_baseline'])} | "
            f"{number(row['delta_sharpe_vs_baseline'])} |"
        )
    lines.append("")
    for experiment_id, group in sorted(groups.items(), key=lambda item: item[1][0]["created_at"], reverse=True):
        first = group[0]
        lines.extend([
            f"## {experiment_id}",
            "",
            f"- 时间：{first['created_at']}；状态：{first['status']}；区间：{first['test_period']}",
            f"- 目的：{first['purpose']}",
            f"- 改动：{first['change_note']}",
            f"- 已知问题：{first['known_limitations']}",
            f"- 证据：`{first['manifest_path']}`；代码：`{first['code_revision']}`；工作区脏：{first['git_dirty']}",
            "",
            "| 策略 | 累计收益 | 夏普 | 最大回撤 | 相对同次基线收益 | 相对同次基线夏普 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ])
        for row in group:
            lines.append(
                f"| {row['strategy']} | {percent(row['cumulative_return'])} | "
                f"{number(row['sharpe'])} | {percent(row['max_drawdown'])} | "
                f"{percent(row['delta_return_vs_baseline'])} | {number(row['delta_sharpe_vs_baseline'])} |"
            )
        lines.append("")
    _REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def register_experiment(metadata, metrics_by_strategy, output_dir):
    """追加结构化登记册并重建阅读版报告；任何单项失败都应使回测显式失败。"""
    rows = _registry_rows(metadata, metrics_by_strategy, output_dir)
    _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing_rows = []
    if _REGISTRY_PATH.exists():
        with _REGISTRY_PATH.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames and reader.fieldnames != _REGISTRY_COLUMNS:
                raise RuntimeError("experiment_index.csv 架构不匹配；请先迁移历史登记册")
            existing_rows = list(reader)
    existing_keys = {(row["experiment_id"], row["strategy"]) for row in existing_rows}
    duplicate_keys = {(row["experiment_id"], row["strategy"]) for row in rows} & existing_keys
    if duplicate_keys:
        raise RuntimeError(f"实验登记重复：{sorted(duplicate_keys)}")
    all_rows = [*existing_rows, *rows]
    with _REGISTRY_PATH.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=_REGISTRY_COLUMNS)
        writer.writeheader()
        writer.writerows(all_rows)
    _render_experiment_report(all_rows)


def _write_report(path, metrics_by_strategy, metadata):
    """写入本次实验的简要 Markdown 摘要，供人工审阅。"""
    lines = [
        "# 回测结果摘要",
        "",
        f"- 运行时间：{metadata['created_at']}",
        f"- 区间：{metadata['test_period']}",
        f"- 股票池：{', '.join(metadata['universe'])}",
        f"- 代码版本：{metadata['code_revision']}",
        "",
        "| 策略 | 累计收益 | 最大回撤 | 夏普 | 年化收益 | 年化波动 | 交易数 | 换手率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, metrics in metrics_by_strategy.items():
        lines.append(
            f"| {name} | {metrics.get('累计收益率', 'N/A')} | "
            f"{metrics.get('最大回撤', 'N/A')} | {metrics.get('夏普比率', 'N/A')} | "
            f"{metrics.get('年化收益率', 'N/A')} | {metrics.get('年化波动率', 'N/A')} | "
            f"{metrics.get('总交易次数', 'N/A')} | {metrics.get('换手率', 'N/A')} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_results(output_dir, results: dict, metadata: dict):
    """保存 CSV、可复现 manifest 与本次实验的 Markdown 摘要。

    Args:
        results: {策略名: Recorder} 字典，按插入顺序输出。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = metadata["run_id"]
    saved = []

    # --- 1. summary.csv：一行一个策略，核心指标对比 ---
    summary_rows = []
    metrics_by_strategy = {}
    for name, rec in results.items():
        if rec and rec.equity_curve:
            m = rec.evaluate()
            metrics_by_strategy[name] = m
            summary_rows.append({"策略": name, **m})

    if summary_rows:
        path = output_dir / f"summary_{ts}.csv"
        pd.DataFrame(summary_rows).to_csv(path, index=False, encoding="utf-8-sig")
        saved.append(path.name)

    # --- 2. equity.csv：合并净值曲线（方便画图） ---
    equity_dfs = []
    for name, rec in results.items():
        if rec and rec.equity_curve:
            col = name.lower().replace("&", "and").replace(" ", "_").replace("__", "_")
            df = pd.DataFrame(rec.equity_curve, columns=["date", col])
            df["date"] = pd.to_datetime(df["date"])
            equity_dfs.append(df)

    if equity_dfs:
        merged = equity_dfs[0]
        for df in equity_dfs[1:]:
            merged = merged.merge(df, on="date", how="outer")
        merged = merged.sort_values("date").reset_index(drop=True)
        path = output_dir / f"equity_{ts}.csv"
        merged.to_csv(path, index=False)
        saved.append(path.name)

    # --- 3. trades.csv：合并交易记录 ---
    all_trades = []
    for name, rec in results.items():
        if rec and rec.trades:
            for t in rec.trades:
                all_trades.append({
                    "策略": name, "日期": t.date, "股票": t.symbol,
                    "方向": t.direction, "价格": f"{t.price:.2f}", "股数": t.shares,
                    "手续费": f"{t.commission:.2f}", "盈亏": f"{t.pnl:.2f}", "原因": t.reason,
                })
    if all_trades:
        path = output_dir / f"trades_{ts}.csv"
        pd.DataFrame(all_trades).to_csv(path, index=False, encoding="utf-8-sig")
        saved.append(path.name)

    # --- 4. decisions.csv：调仓级暴露与回撤诊断 ---
    decision_rows = []
    for name, rec in results.items():
        for record in getattr(rec, "decisions", []):
            decision_rows.append({"策略": name, **record})
    if decision_rows:
        path = output_dir / f"decisions_{ts}.csv"
        pd.DataFrame(decision_rows).to_csv(path, index=False, encoding="utf-8-sig")
        saved.append(path.name)

    report_path = output_dir / f"report_{ts}.md"
    _write_report(report_path, metrics_by_strategy, metadata)
    saved.append(report_path.name)

    manifest = {
        **metadata,
        "output_files": [*saved, "manifest.json"],
        "strategies": list(metrics_by_strategy),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    saved.append(manifest_path.name)

    register_experiment(metadata, metrics_by_strategy, output_dir)

    if saved:
        print(f"\n  结果已保存到 {output_dir}:")
        for name in saved:
            print(f"    - {name}")


def print_summary(results: dict):
    """打印各策略的核心指标汇总对比表。"""
    rows = []
    for name, rec in results.items():
        if rec and rec.equity_curve:
            m = rec.evaluate()
            rows.append((name, m))

    if not rows:
        return

    print(f"\n{'=' * 72}")
    print(f"  {'回测结果汇总':^68}")
    print(f"{'=' * 72}")
    print(f"  {'策略':<14} {'累计收益':>10} {'最大回撤':>10} {'夏普比率':>10} "
          f"{'胜率':>10} {'交易数':>8} {'换手率':>10}")
    print(f"  {'-' * 68}")

    for label, m in rows:
        print(f"  {label:<14} {m.get('累计收益率', 'N/A'):>10} {m.get('最大回撤', 'N/A'):>10} "
              f"{m.get('夏普比率', 'N/A'):>10} {m.get('胜率', 'N/A'):>10} "
              f"{str(m.get('总交易次数', 'N/A')):>8} {m.get('换手率', 'N/A'):>10}")

    print(f"{'=' * 72}")


def main():
    args = parse_args()

    print("\n" + "=" * 60)
    print("  统一回测脚本")
    print("=" * 60)
    print(f"\n  候选池：{args.pool} ({len(args.pool)} 只)")
    print(f"  时间：{args.start} ~ {args.end}")
    print(f"  初始资金：${config.INITIAL_CAPITAL:,.0f}")
    print(f"  Top-K: {config.TOP_K}  调仓周期：{config.REBALANCE_DAYS} 天")

    # 1. 数据采集
    market_data, news_df = download_data(args.pool, args.start, args.end)

    # 2. 各策略回测
    results = {}  # {策略名: Recorder}

    # --- Buy & Hold 等权基准 ---
    print(f"\n  --- Buy & Hold 等权基准 ---")
    results["Buy & Hold"] = create_buy_and_hold_recorder(market_data, config.INITIAL_CAPITAL)
    print(f"  [OK] 等权 B&H 完成")

    # --- LLM Top-K ---
    print(f"\n  --- LLM Top-K ---")

    def llm_decide(date, market_state, candidate_pool, news_df, current_holdings, market_data=None):
        return decide_formula_llm(date, market_state, candidate_pool, news_df, current_holdings, market_data=market_data)

    llm_recorder = run_multi_stock_backtest(llm_decide, market_data, news_df, args.pool)
    if llm_recorder.equity_curve:
        print(f"  [OK] LLM 回测完成，最终权益：${llm_recorder.equity_curve[-1][1]:,.2f}")
    results["LLM Top-K"] = llm_recorder

    # --- PPO Top-K（LLM 选股 + PPO stage1 择时接管总暴露）---
    # 与 LLM Top-K 同一选股，仅把「波动率目标公式暴露」换成 PPO 学出的档位 → 干净 A/B
    print(f"\n  --- PPO Top-K ---")
    ppo_model_path = Path(args.ppo_model) if args.ppo_model else \
        config.PROJECT_ROOT / "models" / "ppo_stage1.zip"
    if ppo_model_path.exists():
        from core.ppo.predict import PPOExposurePredictor
        predictor = PPOExposurePredictor(
            args.pool, args.start, args.end, model_path=str(ppo_model_path),
            device=args.ppo_device)
        ppo_recorder = run_multi_stock_backtest(
            llm_decide, market_data, news_df, args.pool,
            exposure_predictor=predictor.predict_exposure,
        )
        if ppo_recorder.equity_curve:
            print(f"  [OK] PPO 回测完成，最终权益：${ppo_recorder.equity_curve[-1][1]:,.2f}")
        results["PPO Top-K"] = ppo_recorder
    else:
        print(f"  [SKIP] 未找到 PPO 模型：{ppo_model_path}")

    # --- VADER Top-K ---
    if not args.skip_vader:
        print(f"\n  --- VADER Top-K ---")

        def vader_decide(date, market_state, candidate_pool, news_df, current_holdings, market_data=None):
            return decide_formula_vader(date, market_state, candidate_pool, news_df, current_holdings, market_data=market_data)

        vader_recorder = run_multi_stock_backtest(vader_decide, market_data, news_df, args.pool)
        if vader_recorder.equity_curve:
            print(f"  [OK] VADER 回测完成，最终权益：${vader_recorder.equity_curve[-1][1]:,.2f}")
        results["VADER Top-K"] = vader_recorder
    else:
        print(f"\n  [SKIP] VADER")

    # 3. 保存结果
    print("\n" + "=" * 60)
    print("  保存结果")
    print("=" * 60)

    run_id = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir = config.OUTPUT_DIR / f"backtest_{run_id}"
    ppo_model_sha256 = _sha256_file(ppo_model_path) if "PPO Top-K" in results else None
    metadata = {
        "experiment_id": args.experiment_id or f"backtest_{run_id}",
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "code_revision": _git_revision(),
        "test_period": f"{args.start}~{args.end}",
        "universe": args.pool,
        "initial_capital": config.INITIAL_CAPITAL,
        "selection": f"LLM Top-K={config.TOP_K}",
        "position_weights": "inverse volatility",
        "cost_model": {
            "commission_per_order": config.COMMISSION_PER_TRADE,
            "slippage": config.SLIPPAGE,
            "turnover_penalty": config.TURNOVER_PENALTY_RATIO,
        },
        "ppo_model_path": str(ppo_model_path) if "PPO Top-K" in results else None,
        "ppo_model_sha256": ppo_model_sha256,
        "ppo_device": args.ppo_device if "PPO Top-K" in results else None,
        "research": {
            "purpose": args.purpose,
            "change_note": args.change_note,
            "known_limitations": args.known_limitations,
            "control_experiment": args.control_experiment,
            "baseline_strategy": args.baseline_strategy,
            "seed": args.seed,
            "status": args.status,
        },
        "code_snapshot": {
            "git_dirty": bool(_git_output("status", "--porcelain")),
            "git_diff_name_status": _git_output("diff", "--name-status", "HEAD"),
        },
        "data_snapshot": {
            "market_data_sha256": _dataframe_sha256(
                pd.concat({symbol: df for symbol, df in market_data.items()})
            ),
            "news_data_sha256": _dataframe_sha256(news_df),
        },
    }
    save_results(output_dir, results, metadata)

    # 4. 终端汇总
    print_summary(results)

    print("\n" + "=" * 60)
    print("  回测完成!")
    print("=" * 60)


if __name__ == "__main__":
    main()
