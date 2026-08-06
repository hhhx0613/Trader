# 设置系统环境变量（永久生效）
# 使用方法：在 PowerShell 中运行此脚本

Write-Host "正在设置 LLM API Keys..." -ForegroundColor Green

# GLM (智谱 AI) API Key
$glmKey = Read-Host "请输入你的 GLM API Key"
if ($glmKey) {
    [Environment]::SetEnvironmentVariable("GLM_API_KEY", $glmKey, "User")
    Write-Host "✓ GLM_API_KEY 已设置" -ForegroundColor Green
}

# DeepSeek API Key
$deepseekKey = Read-Host "请输入你的 DeepSeek API Key"
if ($deepseekKey) {
    [Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", $deepseekKey, "User")
    Write-Host "✓ DEEPSEEK_API_KEY 已设置" -ForegroundColor Green
}

# OpenAI API Key (可选)
$openaiKey = Read-Host "请输入你的 OpenAI API Key (可选，直接回车跳过)"
if ($openaiKey) {
    [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $openaiKey, "User")
    Write-Host "✓ OPENAI_API_KEY 已设置" -ForegroundColor Green
}

Write-Host "`n✅ 环境变量设置完成！" -ForegroundColor Green
Write-Host "请重启 PowerShell 窗口使设置生效。" -ForegroundColor Yellow
Write-Host "`n验证方法：" -ForegroundColor Cyan
Write-Host "  echo `$env:GLM_API_KEY" -ForegroundColor White
Write-Host "  echo `$env:DEEPSEEK_API_KEY" -ForegroundColor White
