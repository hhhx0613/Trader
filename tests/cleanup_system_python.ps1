# 清理系统 Python 项目依赖脚本
# 使用方法：右键 PowerShell → 以管理员身份运行 → 运行此脚本

Write-Host "=== 清理系统 Python 项目依赖 ===" -ForegroundColor Cyan
Write-Host "将卸载以下包：yfinance, pandas, numpy, matplotlib, requests, ib-insync, gymnasium, torch, vaderSentiment, openai, python-dotenv" -ForegroundColor Yellow
Write-Host ""

$packages = "yfinance", "pandas", "numpy", "matplotlib", "requests", "ib-insync", "gymnasium", "torch", "vaderSentiment", "openai", "python-dotenv"

foreach ($pkg in $packages) {
    Write-Host "卸载 $pkg..." -ForegroundColor Yellow
    D:\python310\python.exe -m pip uninstall -y $pkg
}

Write-Host "`n=== 清理完成 ===" -ForegroundColor Green
Write-Host "验证：D:\python310\python.exe -m pip list" -ForegroundColor Gray
