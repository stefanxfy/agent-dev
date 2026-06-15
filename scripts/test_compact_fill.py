#!/usr/bin/env python3
"""
快速填充脚本：注入大量消息直到触发上下文压缩

用法：
    cd /Users/fanyunxu/Desktop/myproject/agent-dev
    python3 scripts/test_compact_fill.py

可选环境变量：
    AUTOCOMPACT_PCT_OVERRIDE=10    # 10% 即触发压缩（快速测试）
    FILL_TARGET_RATIO=0.95         # 填充到总预算的 95%
"""

import os
import sys
import time
import logging

# 确保能 import agent_core
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_core.context.tokenizer import SimpleTokenCounter
from agent_core.context.budget import ContextBudgetManager, get_effective_context_window
from agent_core.context.compact import CompactOrchestrator, COMPACT_MAX_OUTPUT_TOKENS
from agent_core.context.manager import ContextManager
from agent_core.llm.router import LLMRouter, LLMConfig, LLMProvider

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_fill")

# ── 配置 ──────────────────────────────────────────────────────

MODEL = "glm-4-flash"  # 用 flash 省钱省时间

# 填充目标：占预算的多少比例（默认 95%，确保 should_compact 触发）
TARGET_RATIO = float(os.environ.get("FILL_TARGET_RATIO", "0.95"))

# 每条填充消息的中文长度（字符数）
FILL_MSG_CHARS = 200

# 填充文本模板（200字中文 ≈ 280 tokens + 10 overhead ≈ 290 tokens/条）
FILL_TEXT_TEMPLATE = (
    "这是一段用于测试上下文压缩功能的填充文本。"
    "人工智能是计算机科学的一个分支，它企图了解智能的实质，"
    "并生产出一种新的能以人类智能相似的方式做出反应的智能机器。"
    "该领域的研究包括机器人、语言识别、图像识别、自然语言处理和专家系统等。"
    "人工智能从诞生以来，理论和技术日益成熟，应用领域也不断扩大，"
    "可以设想，未来人工智能带来的科技产品，将会是人类智慧的容器。"
    "人工智能可以对人的意识、思维的信息过程的模拟。"
    "人工智能不是人的智能，但能像人那样思考，也可能超过人的智能。"
)


def build_llm_router() -> LLMRouter:
    """构建 LLM Router（使用智谱 API）"""
    api_key = os.environ.get("ZHIPU_API_KEY", "")
    if not api_key:
        # 尝试从 .env 读取
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
        )
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ZHIPU_API_KEY="):
                        api_key = line.split("=", 1)[1].strip()
                        break

    if not api_key:
        logger.error("ZHIPU_API_KEY not found! Set it in .env or environment.")
        sys.exit(1)

    config = LLMConfig(
        provider=LLMProvider.ZHIPU,
        api_key=api_key,
        model=MODEL,
    )
    return LLMRouter(config)


def generate_fill_messages(target_tokens: int, tokens_per_msg: int) -> list[dict]:
    """生成填充消息列表"""
    num_messages = int(target_tokens / tokens_per_msg) + 10  # 多加 10 条确保超阈值
    messages = []

    # 1 条 system prompt
    messages.append({
        "role": "system",
        "content": "你是一个有用的AI助手。请用中文回答问题。",
    })

    # 交替 user/assistant 填充
    for i in range(num_messages):
        role = "user" if i % 2 == 0 else "assistant"
        # 每条消息加序号，避免重复
        content = f"[消息 #{i+1}] {FILL_TEXT_TEMPLATE}"
        messages.append({"role": role, "content": content})

    return messages


def main():
    print("=" * 60)
    print("上下文压缩 - 快速填充测试")
    print("=" * 60)

    # ── 1. 初始化组件 ─────────────────────────────────────
    counter = SimpleTokenCounter()

    # 检查是否有比例覆盖
    env_pct = os.environ.get("AUTOCOMPACT_PCT_OVERRIDE", "").strip()
    if env_pct:
        print(f"\n⚡ AUTOCOMPACT_PCT_OVERRIDE = {env_pct}%")

    effective_window = get_effective_context_window(MODEL)
    print(f"\n📊 模型: {MODEL}")
    print(f"📊 有效窗口: {effective_window:,} tokens")
    print(f"📊 填充目标: {TARGET_RATIO:.0%} = {int(effective_window * TARGET_RATIO):,} tokens")

    # 触发阈值
    from agent_core.context.budget import AUTOCOMPACT_BUFFER_TOKENS
    trigger_threshold = effective_window - AUTOCOMPACT_BUFFER_TOKENS
    print(f"📊 压缩触发线: used > {trigger_threshold:,} tokens (available < {AUTOCOMPACT_BUFFER_TOKENS:,})")

    # ── 2. 生成填充消息 ───────────────────────────────────
    target_tokens = int(effective_window * TARGET_RATIO)
    # 每条消息 ≈ 200字中文 × 1.4 + 10 overhead ≈ 290 tokens
    tokens_per_msg = 290
    messages = generate_fill_messages(target_tokens, tokens_per_msg)

    # 计算实际 tokens
    actual_tokens = counter.count_messages(messages)
    print(f"\n📝 生成消息: {len(messages)} 条")
    print(f"📝 估算 tokens: {actual_tokens:,}")

    # ── 3. 检查预算状态（压缩前）──────────────────────────
    budget_mgr = ContextBudgetManager(MODEL, counter)
    should, reason = budget_mgr.should_compact(messages)
    usage = budget_mgr.get_usage_info(messages)

    print(f"\n{'='*60}")
    print("压缩前预算状态:")
    print(f"  总预算: {usage['total_budget']:,}")
    print(f"  已使用: {usage['used_tokens']:,}")
    print(f"  可用:   {usage['available_tokens']:,}")
    print(f"  使用率: {usage['usage_ratio']:.1%}")
    print(f"  should_compact: {should} ({reason})")
    print(f"  is_critical: {usage['is_critical']}")
    print(f"{'='*60}")

    if not should:
        print("\n⚠️ 未达到压缩阈值！需要更多消息。")
        # 自动追加更多
        print("自动追加消息...")
        while not should:
            for _ in range(50):
                idx = len(messages)
                role = "user" if idx % 2 == 0 else "assistant"
                messages.append({"role": role, "content": f"[追加 #{idx}] {FILL_TEXT_TEMPLATE}"})
            actual_tokens = counter.count_messages(messages)
            should, reason = budget_mgr.should_compact(messages)
            print(f"  消息 {len(messages)} 条, {actual_tokens:,} tokens → should_compact={should}")

    # ── 4. 执行压缩 ────────────────────────────────────────
    print(f"\n🔧 初始化 LLM Router (模型: {MODEL})...")
    llm = build_llm_router()

    print("🔧 初始化 ContextManager...")
    cm = ContextManager(llm_router=llm, model=MODEL)

    print(f"\n{'='*60}")
    print("开始压缩...")
    print(f"{'='*60}")

    t0 = time.time()
    compacted, result = cm.check_and_compact(messages)
    elapsed = time.time() - t0

    # ── 5. 报告结果 ────────────────────────────────────────
    print(f"\n{'='*60}")
    print("压缩结果:")
    print(f"{'='*60}")

    if result is None:
        print("❌ 未触发压缩（should_compact=False）")
    elif result.success:
        print(f"✅ 压缩成功!")
        print(f"  压缩前: {result.tokens_before:,} tokens")
        print(f"  压缩后: {result.tokens_after:,} tokens")
        print(f"  释放:   {result.tokens_freed:,} tokens ({result.tokens_freed/result.tokens_before:.1%})")
        print(f"  PTL重试: {result.ptl_retries} 次")
        print(f"  耗时:   {result.compact_time_ms:.0f} ms ({elapsed:.1f}s 总)")
        print(f"  压缩后消息数: {len(compacted)} 条 (原 {len(messages)} 条)")
        print()
        print("  压缩后消息结构:")
        for i, msg in enumerate(compacted):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            tokens = counter.count(content) + 10
            preview = content[:80].replace("\n", " ") + ("..." if len(content) > 80 else "")
            print(f"    [{i}] {role}: {tokens} tokens — {preview}")

        # 压缩后的预算状态
        post_usage = cm.get_usage_info(compacted)
        print(f"\n  压缩后预算:")
        print(f"    已使用: {post_usage['used_tokens']:,} / {post_usage['total_budget']:,}")
        print(f"    可用:   {post_usage['available_tokens']:,}")
        print(f"    使用率: {post_usage['usage_ratio']:.1%}")
        print(f"    should_compact: {post_usage['should_compact']}")

        # 摘要内容预览
        if result.summary:
            print(f"\n  摘要预览 (前 500 字):")
            print(f"    {result.summary[:500]}")
    else:
        print(f"❌ 压缩失败: {result.error}")
        print(f"  耗时: {result.compact_time_ms:.0f} ms")

    # ── 6. 统计 ────────────────────────────────────────────
    stats = cm.get_stats()
    print(f"\n{'='*60}")
    print("ContextManager 统计:")
    print(f"  模型: {stats['model']}")
    print(f"  总预算: {stats['total_budget']:,}")
    print(f"  压缩次数: {stats['compact_count']}")
    print(f"  总释放: {stats['total_tokens_freed']:,} tokens")
    print(f"  连续失败: {stats['consecutive_failures']}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
