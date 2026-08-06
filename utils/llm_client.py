"""
统一的 LLM 客户端

支持通过 OpenAI 兼容接口灵活切换：
- OpenAI (GPT-4, GPT-3.5-turbo)
- GLM (智谱 AI: GLM-4, GLM-3-turbo)
- DeepSeek (DeepSeek-Chat, DeepSeek-Code)

使用方法：
  client = LLMClient(provider="glm")  # 或 "openai", "deepseek"
  response = client.chat("你好")
  
设计说明：
  - GLM 和 DeepSeek 都兼容 OpenAI 接口
  - 只需要切换 base_url 和 api_key
  - 统一使用 openai 库调用
"""

import os
from typing import Dict, List, Optional
from pathlib import Path

from core import config


class LLMClient:
    """
    统一的 LLM 客户端
    
    支持的提供商：
    - "openai": OpenAI 官方 API
    - "glm": 智谱 AI (GLM-4)
    - "deepseek": DeepSeek
    """
    
    # 各提供商的配置
    PROVIDERS = {
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
            "default_model": "gpt-4",
            "models": ["gpt-4", "gpt-3.5-turbo"],
        },
        "glm": {
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "api_key_env": "GLM_API_KEY",
            "default_model": "glm-4",
            "models": ["glm-4", "glm-3-turbo"],
        },
        "deepseek": {
            "base_url": "https://api.deepseek.com/v1",
            "api_key_env": "DEEPSEEK_API_KEY",
            "default_model": "deepseek-chat",
            "models": ["deepseek-chat", "deepseek-code"],
        },
    }
    
    def __init__(self, provider: Optional[str] = None, model: Optional[str] = None):
        """
        参数：
          provider: LLM 提供商 ("openai", "glm", "deepseek")
            如果为 None，则从环境变量 DEFAULT_LLM_PROVIDER 读取
          model: 模型名称
            如果为 None，则使用该提供商的默认模型
        """
        # 确定提供商
        if provider is None:
            provider = os.getenv("DEFAULT_LLM_PROVIDER", "openai")
        
        if provider not in self.PROVIDERS:
            raise ValueError(f"不支持的提供商：{provider}，可选：{list(self.PROVIDERS.keys())}")
        
        self.provider = provider
        self.provider_config = self.PROVIDERS[provider]
        
        # 获取 API Key
        api_key_env = self.provider_config["api_key_env"]
        self.api_key = os.getenv(api_key_env)
        
        if not self.api_key or self.api_key == f"your_{api_key_env.lower()}_here":
            raise ValueError(
                f"未配置 {api_key_env}，请在 .env 文件中设置\n"
                f"或者运行：export {api_key_env}=your_key_here"
            )
        
        # 确定模型
        self.model = model or self.provider_config["default_model"]
        
        # 初始化 OpenAI 客户端
        try:
            import openai
            self.client = openai.OpenAI(
                api_key=self.api_key,
                base_url=self.provider_config["base_url"],
            )
        except ImportError:
            raise ImportError("未安装 openai 库，请运行：pip install openai")
    
    def chat(
        self,
        message: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
    ) -> str:
        """
        发送聊天消息，返回回复文本。
        
        参数：
          message: 用户消息
          system_prompt: 系统提示词（可选）
          temperature: 温度参数（0.0-2.0）
          max_tokens: 最大返回 token 数
        
        返回：
          模型回复的文本内容
        """
        messages = []
        
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        
        messages.append({"role": "user", "content": message})
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        
        return response.choices[0].message.content
    
    def chat_json(
        self,
        message: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 1000,
    ) -> Dict:
        """
        发送聊天消息，返回 JSON 解析结果。
        
        参数和返回：
          同 chat()，但返回解析后的 JSON 字典
          如果解析失败，返回 {"error": "JSON 解析失败", "raw": response_text}
        """
        import json
        import re
        
        response_text = self.chat(
            message=message,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        
        try:
            # 尝试直接解析
            return json.loads(response_text)
        except json.JSONDecodeError:
            # 尝试提取 JSON 部分
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except json.JSONDecodeError:
                    pass
            
            # 解析失败
            return {
                "error": "JSON 解析失败",
                "raw": response_text,
                "provider": self.provider,
                "model": self.model,
            }
    
    def get_info(self) -> Dict:
        """
        获取当前客户端信息。
        """
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.provider_config["base_url"],
            "available_models": self.provider_config["models"],
        }


# ==================== 便捷函数 ====================

def create_llm_client(provider: Optional[str] = None, model: Optional[str] = None) -> LLMClient:
    """
    创建 LLM 客户端的便捷函数。
    """
    return LLMClient(provider=provider, model=model)


def quick_chat(message: str, provider: Optional[str] = None, model: Optional[str] = None) -> str:
    """
    快速聊天的便捷函数。
    """
    client = LLMClient(provider=provider, model=model)
    return client.chat(message)


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("测试 LLM 客户端...")
    
    # 测试 GLM
    try:
        print("\n[测试 1] GLM-4...")
        client = LLMClient(provider="glm", model="glm-4")
        print(f"  客户端信息：{client.get_info()}")
        response = client.chat("你好，请用一句话介绍自己")
        print(f"  ✓ 回复：{response[:100]}...")
    except Exception as e:
        print(f"  ✗ 失败：{e}")
    
    # 测试 DeepSeek
    try:
        print("\n[测试 2] DeepSeek...")
        client = LLMClient(provider="deepseek", model="deepseek-chat")
        print(f"  客户端信息：{client.get_info()}")
        response = client.chat("你好，请用一句话介绍自己")
        print(f"  ✓ 回复：{response[:100]}...")
    except Exception as e:
        print(f"  ✗ 失败：{e}")
    
    # 测试 OpenAI（如果有 Key）
    try:
        print("\n[测试 3] OpenAI...")
        client = LLMClient(provider="openai", model="gpt-3.5-turbo")
        print(f"  客户端信息：{client.get_info()}")
        response = client.chat("你好，请用一句话介绍自己")
        print(f"  ✓ 回复：{response[:100]}...")
    except Exception as e:
        print(f"  ✗ 失败：{e}")
    
    print("\n✅ 测试完成！")
