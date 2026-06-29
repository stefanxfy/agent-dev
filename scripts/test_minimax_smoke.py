"""MiniMax (MiniMax) provider 真 API smoke test

验证 4 件事:
1. 客户端能建(base_url + api_key 正确)
2. 流式 chat 能调通(基本对话)
3. UsageStats 能解析(input/output/cached_tokens)
4. system_prompt_override 能正常注入

需要:
- .env 含 MINIMAX_API_KEY
- .venv 已装 openai>=1.30.0

跑法:.venv/bin/python scripts/test_minimax_smoke.py

Stage 4 后的差异:
- `LLMRouter._get_openai_provider` 改为 `ProviderRegistry.create`(稳定 classmethod 锚点)
- `MiniMaxProvider` 走 OpenAI 兼容协议,system_prompt 注入 messages 由 provider 自己处理
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 强制从项目根加载 .env
project_root = Path(__file__).resolve().parent.parent
os.chdir(project_root)
sys.path.insert(0, str(project_root))  # 让 agent_core 可被 import

from dotenv import load_dotenv  # noqa: E402
load_dotenv(project_root / ".env")

from agent_core.llm import (  # noqa: E402
    LLMRouter, LLMConfig, LLMProvider, LLMModel, StreamChunk, TextDelta,
)
from agent_core.llm.registry import ProviderRegistry  # noqa: E402
from agent_core.llm.providers.base import BaseProvider  # noqa: E402


def main() -> int:
    api_key = os.environ.get("MINIMAX_API_KEY", "")
    if not api_key:
        print("❌ .env 里没 MINIMAX_API_KEY,先填上")
        return 1

    print(f"✅ MINIMAX_API_KEY 已加载(长度 {len(api_key)})")

    # ── Test 1: 客户端构造 ──
    print("\n[1/4] 构造 minimax client...")
    cfg = LLMConfig(
        provider=LLMProvider.MINIMAX,
        model="MiniMax-M3",  # 走 env DEFAULT_MODEL 同名
        max_tokens=256,
        temperature=0.0,  # 关掉随机性,易复现
    )
    router = LLMRouter(cfg)
    # Stage 4 后走 provider lazy property → ProviderRegistry.create
    client = router.provider.client
    print(f"   base_url = {client.base_url}")
    assert "api.minimaxi.com" in str(client.base_url), \
        f"base_url 异常: {client.base_url}"
    print("   ✅ base_url 正确")

    # ── Test 2: 流式 chat ──
    print("\n[2/4] 流式 chat 调用...")
    messages = [{"role": "user", "content": "用一句话介绍你自己,不超过 20 字。"}]
    collected_text = ""
    collected_thinking = ""
    collected_usage = None
    chunk_count = 0
    thinking_chunk_count = 0
    text_chunk_count = 0
    try:
        for chunk in router.chat(messages=messages):
            chunk_count += 1
            if chunk.thinking_delta and chunk.thinking_delta.thinking:
                collected_thinking += chunk.thinking_delta.thinking
                thinking_chunk_count += 1
            if chunk.text_delta and chunk.text_delta.text:
                collected_text += chunk.text_delta.text
                text_chunk_count += 1
            if chunk.usage:
                collected_usage = chunk.usage
    except Exception as e:
        print(f"   ❌ chat() 抛异常: {type(e).__name__}: {e}")
        return 1

    print(f"   总 chunks: {chunk_count} (thinking={thinking_chunk_count}, text={text_chunk_count})")
    print(f"   思考过程: {collected_thinking[:80]!r}{'...' if len(collected_thinking) > 80 else ''}")
    print(f"   收到文本: {collected_text!r}")
    if not collected_text:
        print("   ❌ 没收到任何文本,可能 MiniMax-M3 不支持或 model name 错")
        return 1
    # 关键:确认 <think> 标签已经被 splitter 正确分离(原文不应再含 <think>)
    if "<think>" in collected_text or "</think>" in collected_text:
        print(f"   ❌ text_delta 里还残留 <think> 标签,splitter 没工作")
        return 1
    print("   ✅ 流式响应正常,thinking 与 text 已分离")

    # ── Test 3: UsageStats 解析 ──
    print("\n[3/4] UsageStats 解析...")
    if collected_usage is None:
        print("   ⚠️  本次没收到 usage chunk(可能 model 不返回)")
    else:
        print(f"   {collected_usage.summary('minimax')}")
        assert collected_usage.input_tokens > 0, "input_tokens 应 > 0"
        print("   ✅ usage 解析正确")

    # ── Test 4: system_prompt_override 注入 ──
    print("\n[4/4] system_prompt_override 注入...")
    captured_msgs = None
    # Stage 4:monkey-patch ProviderRegistry.create(稳定 classmethod 锚点,不受懒加载影响)
    original = ProviderRegistry.create
    def _mock_create(config):
        class _FakeProvider(BaseProvider):
            provider_name = "fake-minimax"
            def _do_chat(self_, messages, tools=None, tool_choice=None,
                         system_prompt=None, cache_namespace=None):
                nonlocal captured_msgs
                # 模拟 OpenAI 兼容 provider 的 system 注入行为
                if system_prompt:
                    messages = [{"role": "system", "content": system_prompt}, *messages]
                captured_msgs = messages
                # 返回最小可用 chunk,避免真发请求
                return iter([StreamChunk(text_delta=TextDelta(text="ok"))])
        return _FakeProvider(config)
    ProviderRegistry.create = classmethod(lambda cls, config: _mock_create(config))
    try:
        list(router.chat(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt_override="你是一只猫,叫 Mia,只回答喵喵相关的问题。",
        ))
    finally:
        ProviderRegistry.create = original
    assert captured_msgs is not None, "override 路径没被调用"
    assert captured_msgs[0]["role"] == "system", \
        f"override 必须注入到首位,实际: {captured_msgs[0]}"
    assert "Mia" in captured_msgs[0]["content"], "override 内容应原样保留"
    print(f"   注入位置: msgs[0] = {captured_msgs[0]}")
    print("   ✅ system_prompt_override 注入正确")

    print("\n🎉 全部通过 — MiniMax provider 接入完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
