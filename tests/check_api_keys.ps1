# 检查 API Key 配置（PowerShell 版本）

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  API Key 配置检查" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

Write-Host "`n【数据源 API Keys】" -ForegroundColor Yellow

# Finnhub
$finnhubKey = $env:FINNHUB_API_KEY
if ($finnhubKey -and $finnhubKey -ne "your_finnhub_key_here") {
    $display = if ($finnhubKey.Length -gt 10) { $finnhubKey.Substring(0, 10) + "..." } else { $finnhubKey }
    Write-Host "  ✓ FINNHUB_API_KEY: $display" -ForegroundColor Green
} else {
    Write-Host "  ✗ FINNHUB_API_KEY: 未配置（必需）" -ForegroundColor Red
}

# Alpha Vantage
$alphaKey = $env:ALPHA_VANTAGE_API_KEY
if ($alphaKey -and $alphaKey -ne "your_alpha_vantage_key_here") {
    $display = if ($alphaKey.Length -gt 10) { $alphaKey.Substring(0, 10) + "..." } else { $alphaKey }
    Write-Host "  ✓ ALPHA_VANTAGE_API_KEY: $display" -ForegroundColor Green
} else {
    Write-Host "  ○ ALPHA_VANTAGE_API_KEY: 未配置（可选）" -ForegroundColor Gray
}

Write-Host "`n【LLM API Keys】" -ForegroundColor Yellow

# GLM
$glmKey = $env:GLM_API_KEY
if ($glmKey -and $glmKey -ne "your_glm_key_here") {
    $display = if ($glmKey.Length -gt 10) { $glmKey.Substring(0, 10) + "..." } else { $glmKey }
    Write-Host "  ✓ GLM_API_KEY: $display" -ForegroundColor Green
} else {
    Write-Host "  ○ GLM_API_KEY: 未配置（可选）" -ForegroundColor Gray
}

# DeepSeek
$deepseekKey = $env:DEEPSEEK_API_KEY
if ($deepseekKey -and $deepseekKey -ne "your_deepseek_key_here") {
    $display = if ($deepseekKey.Length -gt 10) { $deepseekKey.Substring(0, 10) + "..." } else { $deepseekKey }
    Write-Host "  ✓ DEEPSEEK_API_KEY: $display" -ForegroundColor Green
} else {
    Write-Host "  ○ DEEPSEEK_API_KEY: 未配置（可选）" -ForegroundColor Gray
}

# OpenAI
$openaiKey = $env:OPENAI_API_KEY
if ($openaiKey -and $openaiKey -ne "your_openai_key_here") {
    $display = if ($openaiKey.Length -gt 10) { $openaiKey.Substring(0, 10) + "..." } else { $openaiKey }
    Write-Host "  ✓ OPENAI_API_KEY: $display" -ForegroundColor Green
} else {
    Write-Host "  ○ OPENAI_API_KEY: 未配置（可选）" -ForegroundColor Gray
}

Write-Host "`n【LLM 配置】" -ForegroundColor Yellow
$provider = $env:DEFAULT_LLM_PROVIDER
if ($provider) {
    Write-Host "  默认提供商：$provider" -ForegroundColor Cyan
} else {
    Write-Host "  默认提供商：openai（未设置，使用默认值）" -ForegroundColor Gray
}

Write-Host "`n========================================" -ForegroundColor Cyan

# 判断能否测试
$canTestLLM = ($glmKey -and $glmKey -ne "your_glm_key_here") -or 
              ($deepseekKey -and $deepseekKey -ne "your_deepseek_key_here") -or
              ($openaiKey -and $openaiKey -ne "your_openai_key_here")

if ($canTestLLM) {
    Write-Host "  ✅ 至少配置了一个 LLM API Key，可以运行测试！" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "`n下一步：" -ForegroundColor Yellow
    Write-Host "  1. 运行 LLM 测试：python tests/test_stage2.py" -ForegroundColor White
    Write-Host "  2. 运行真实 LLM 测试：python utils/llm_client.py" -ForegroundColor White
} else {
    Write-Host "  ⚠️  未配置任何 LLM API Key" -ForegroundColor Yellow
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "`n配置方法：" -ForegroundColor Yellow
    Write-Host "  方法 1: 设置系统环境变量（推荐）" -ForegroundColor White
    Write-Host "    运行：.\set_env_keys.ps1" -ForegroundColor Gray
    Write-Host "  方法 2: 编辑 .env 文件" -ForegroundColor White
    Write-Host "    填入：GLM_API_KEY=xxx" -ForegroundColor Gray
}

Write-Host "========================================" -ForegroundColor Cyan
