"""
测试 API Key 配置

检查顺序：
1. 系统环境变量（优先）
2. .env 文件（备用）
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✓ python-dotenv 已安装，.env 文件已加载")
except ImportError:
    print("⚠️  python-dotenv 未安装，只读取系统环境变量")


def check_key(name, required=False):
    """检查 API Key 是否配置。"""
    value = os.getenv(name)
    
    if value and value != f"your_{name.lower()}_here":
        # 显示前 10 位
        display = value[:10] + "..." if len(value) > 10 else value
        print(f"  ✓ {name}: {display}")
        return True
    else:
        if required:
            print(f"  ✗ {name}: 未配置（必需）")
        else:
            print(f"  ○ {name}: 未配置（可选）")
        return False


print("=" * 60)
print("  API Key 配置检查")
print("=" * 60)

print("\n【数据源 API Keys】")
check_key("FINNHUB_API_KEY", required=True)
check_key("ALPHA_VANTAGE_API_KEY", required=False)

print("\n【LLM API Keys】")
check_key("GLM_API_KEY", required=False)
check_key("DEEPSEEK_API_KEY", required=False)
check_key("OPENAI_API_KEY", required=False)

print("\n【LLM 配置】")
provider = os.getenv("DEFAULT_LLM_PROVIDER", "openai")
print(f"  默认提供商：{provider}")

print("\n" + "=" * 60)

# 判断能否运行测试
can_test_llm = any([
    os.getenv("GLM_API_KEY") and os.getenv("GLM_API_KEY") != "your_glm_key_here",
    os.getenv("DEEPSEEK_API_KEY") and os.getenv("DEEPSEEK_API_KEY") != "your_deepseek_key_here",
    os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_API_KEY") != "your_openai_key_here",
])

if can_test_llm:
    print("  ✅ 至少配置了一个 LLM API Key，可以运行测试！")
    print("=" * 60)
    print("\n下一步：")
    print("  1. 运行 LLM 测试：python tests/test_stage2.py")
    print("  2. 运行 LLM 客户端测试：python utils/llm_client.py")
else:
    print("  ⚠️  未配置任何 LLM API Key")
    print("=" * 60)
    print("\n请选择：")
    print("  A. 先配置 API Key 再测试")
    print("\n配置方法：")
    print("  方法 1: 设置系统环境变量（推荐）")
    print("    运行：.\\set_env_keys.ps1")
    print("  方法 2: 编辑 .env 文件")
    print("    填入：GLM_API_KEY=xxx")

print("=" * 60)
