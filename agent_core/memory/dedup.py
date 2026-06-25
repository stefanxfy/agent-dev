"""
语义去重决策(向量召回 + LLM 判定)

职责单一:给定「候选记忆与库内最相似记忆的相似度」,决定该走哪条路。
纯函数,不碰向量库 / LLM —— 便于单测,也让 channel B 只负责编排。

三档(阈值见 DedupConfig):
  - 相似度 >= auto_threshold        → AUTO_DUPLICATE(直接判重复,不调 LLM)
  - judge_floor <= 相似度 < auto    → NEEDS_JUDGE(调一次 LLM 判重复/新增)
  - 相似度 < judge_floor / 无召回     → NEW(视为新记忆,正常写盘)

设计依据(bge-m3 实测,短句):
  否定对(喜欢/不喜欢) 0.77~0.89、近义事实(周杰伦/周深) 0.84,
  逐字改写重复 0.95~0.98。0.95 阈值能抓住逐字改写、又不误并否定/近义。
  0.85~0.95 这段真重复与否定/近义混叠,只能交 LLM 判。
"""
from __future__ import annotations

import logging as _logging
from enum import Enum
from typing import Optional, Protocol, Any

_log = _logging.getLogger("memory.dedup")


class DedupAction(str, Enum):
    AUTO_DUPLICATE = "auto_duplicate"  # 相似度极高 → 直接跳过(省 LLM)
    NEEDS_JUDGE = "needs_judge"        # 可疑带 → 交 LLM 判
    NEW = "new"                        # 不够相似 → 当新记忆写


class _DedupThresholds(Protocol):
    """DedupConfig 的最小接口(只用到这几个字段)"""
    enabled: bool
    auto_threshold: float
    judge_floor: float
    top_k: int


class DedupJudge(Protocol):
    """LLM 去重判定器:候选 vs 召回到的相似记忆 → True=重复(应跳过)。

    similar 形如 vector_store.query 的返回:
        [{"id", "metadata": {"title", ...}, "document", "distance"}, ...]
    """
    def __call__(self, candidate: Any, similar: list[dict]) -> bool: ...


def similarity_from_distance(distance: float) -> float:
    """Chroma cosine distance → cosine similarity(distance = 1 - sim)。

    钳到 [0, 1],防浮点误差(偶发 -1e-9 / 1.0000001)。
    """
    sim = 1.0 - float(distance)
    if sim < 0.0:
        return 0.0
    if sim > 1.0:
        return 1.0
    return sim


def top_similarity(hits: list[dict]) -> Optional[float]:
    """从 vector_store.query 的结果取最高相似度(hits 按 distance 升序)。

    无召回 → None。
    """
    if not hits:
        _log.debug("向量召回: 无命中结果")
        return None

    top_sim = similarity_from_distance(hits[0].get("distance", 1.0))
    # DEBUG: 记录召回结果
    _log.debug(
        f"向量召回: 命中 {len(hits)} 条, top_sim={top_sim:.4f}, "
        f"titles={[(h.get('metadata') or {}).get('title', '?')[:30] for h in hits]}"
    )
    return top_sim


def decide_action(top_sim: Optional[float], cfg: _DedupThresholds) -> DedupAction:
    """根据最高相似度选择动作。top_sim=None(无召回)→ NEW。"""
    if top_sim is None:
        _log.debug(f"去重决策: 无召回 → NEW")
        return DedupAction.NEW
    if top_sim >= cfg.auto_threshold:
        _log.debug(
            f"去重决策: sim={top_sim:.4f} >= {cfg.auto_threshold} → AUTO_DUPLICATE"
        )
        return DedupAction.AUTO_DUPLICATE
    if top_sim >= cfg.judge_floor:
        _log.debug(
            f"去重决策: {cfg.judge_floor} <= sim={top_sim:.4f} < {cfg.auto_threshold} → NEEDS_JUDGE"
        )
        return DedupAction.NEEDS_JUDGE
    _log.debug(f"去重决策: sim={top_sim:.4f} < {cfg.judge_floor} → NEW")
    return DedupAction.NEW


def make_llm_dedup_judge(
    llm_router: Any, *, cache_namespace: str = "memory_dedup_judge"
) -> DedupJudge:
    """构造一个基于 LLM 的去重判定器(可疑带才会被调用)。

    llm_router 只需有 chat(messages, **kw) → 流式 chunk(chunk.text_delta.text)。
    判定失败(LLM 异常 / JSON 解析失败)→ 返回 False(不当重复,宁可多存不可误删)。
    """
    import json
    import logging as _logging

    _log = _logging.getLogger("memory.dedup")

    def judge(candidate: Any, similar: list[dict]) -> bool:
        # 延迟 import 避免循环依赖(extraction_gate ← dual_channel_writer ← dedup)
        from agent_core.memory.prompt_templates import (
            DEDUP_SYSTEM_PROMPT,
            build_dedup_prompt,
        )
        from agent_core.memory.extraction_gate import _strip_code_fence

        cand_text = f"{getattr(candidate, 'title', '')}\n{getattr(candidate, 'body', '')}"
        _log.debug(
            f"LLM 去重判定开始: 候选=[{getattr(candidate, 'type', '')}] "
            f"{getattr(candidate, 'title', '')!r}, 已召回 {len(similar)} 条相似记忆"
        )

        prompt = build_dedup_prompt(cand_text, similar)
        text = ""
        try:
            for chunk in llm_router.chat(
                messages=[
                    {"role": "system", "content": DEDUP_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                cache_namespace=cache_namespace,
            ):
                if chunk.text_delta:
                    text += chunk.text_delta.text
            data = json.loads(_strip_code_fence(text))
            is_dup = bool(data.get("is_duplicate", False))
            reason = data.get("reason", "")[:100]  # 截断避免日志过长
            _log.info(
                f"LLM 去重判定结果: is_duplicate={is_dup}, reason={reason!r}"
            )
            return is_dup
        except Exception as e:
            _log.warning(
                f"LLM 去重判定失败(放行,不当重复): {type(e).__name__}: {e}"
            )
            return False

    return judge
