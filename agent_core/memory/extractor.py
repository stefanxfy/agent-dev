"""
记忆提取器（v2.1 §九 L1 修复）

M3 / Day 3 — L1 合并

设计要点：
1. **L1 合并**：对 LLM 一次提取的多个 candidates,合并相似项
   - 同 type + 高度相似 body → 留最长的（信息更多）
   - 完全相同 (type, body, source_quote) → 去重（A5 兜底）
2. **L7 校验**：source_quote 必填（无源 = 假记忆）
3. **L8 过滤**：body/source_quote 含密钥 → 丢弃
4. **type 校验**：必须是 5 类之一
5. **可插拔合并策略**：
   - 默认: 简单 Jaccard 相似度
   - 可选: 嵌入相似度（需要 embed_fn）

调用入口:
    extractor = MemoryExtractor(embed_fn=None)  # 不用嵌入
    cleaned = extractor.process(raw_candidates)
    # → list[ExtractionCandidate] (validated, merged, safe)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from agent_core.exceptions import AgentError
from agent_core.memory.dual_channel_writer import ExtractionCandidate
from agent_core.memory.secret_scanner import SecretScanner, get_default_scanner
from agent_core.memory.types import validate_type

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────

class ExtractorError(AgentError):
    """提取器异常"""
    code = "EXTRACTOR"


class CandidateRejected(ExtractorError):
    """单个 candidate 被拒（L7/L8/类型无效）"""
    code = "CANDIDATE_REJECTED"


# ──────────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────────

@dataclass
class ExtractStats:
    """提取处理统计"""
    input_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    secret_filtered: int = 0
    merged_count: int = 0
    rejection_reasons: dict[str, int] = None

    def __post_init__(self):
        if self.rejection_reasons is None:
            self.rejection_reasons = {}

    def summary(self) -> str:
        return (
            f"ExtractStats: in={self.input_count} accepted={self.accepted_count} "
            f"rejected={self.rejected_count} (secret={self.secret_filtered}) "
            f"merged={self.merged_count}"
        )


# ──────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────

def _normalize_text(text: str) -> str:
    """归一化用于相似度比较: 去空白 + 小写"""
    return re.sub(r"\s+", "", text or "").lower()


def _jaccard(a: str, b: str) -> float:
    """
    Jaccard 相似度（按字符 bigram 集合）

    J(A,B) = |A∩B| / |A∪B|
    """
    if not a or not b:
        return 0.0
    a_bigrams = {a[i:i+2] for i in range(len(a) - 1)}
    b_bigrams = {b[i:i+2] for i in range(len(b) - 1)}
    if not a_bigrams or not b_bigrams:
        return 0.0
    intersection = a_bigrams & b_bigrams
    union = a_bigrams | b_bigrams
    return len(intersection) / len(union)


def _containment(a: str, b: str) -> float:
    """
    包含度: 较短的字符串在较长字符串中的覆盖比例

    containment(A, B) = |A∩B| / |A|（A 是较短的）
    """
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a  # a = shorter
    a_bigrams = {a[i:i+2] for i in range(len(a) - 1)}
    if not a_bigrams:
        return 0.0
    b_bigrams = {b[i:i+2] for i in range(len(b) - 1)}
    intersection = a_bigrams & b_bigrams
    return len(intersection) / len(a_bigrams)


def _similarity_combined(a: str, b: str) -> float:
    """
    综合相似度: max(jaccard, containment)

    - jaccard: 集合重叠度(适合编辑类变化)
    - containment: 包含度(适合前缀/后缀 + 扩展)
    """
    return max(_jaccard(a, b), _containment(a, b))


# ──────────────────────────────────────────────────────────────────
# MemoryExtractor
# ──────────────────────────────────────────────────────────────────

class MemoryExtractor:
    """
    记忆提取器（M3 L1 合并）

    用法:
        extractor = MemoryExtractor()
        cleaned = extractor.process(candidates, stats=stats)
        # → 合并 / 过滤 / 校验后,可直接喂给 MemoryStore.write
    """

    # 合并阈值: 相似度 > 此值视为重复
    # 注: _similarity_combined = max(jaccard, containment)
    #    前缀类 body (A 是 B 子串) 触发 containment=1.0 → 必合并
    #    轻微编辑触发 jaccard >= 0.5 → 合并
    #    完全不同的 body 不会触发
    MERGE_THRESHOLD = 0.6

    def __init__(
        self,
        embed_fn=None,                      # 可选,用于嵌入相似度合并
        secret_scanner: Optional[SecretScanner] = None,
        merge_threshold: float = MERGE_THRESHOLD,
    ):
        """
        Args:
            embed_fn: 可选嵌入函数,若提供则用 cos similarity 合并
                      (不提供则用字符 bigram Jaccard)
            secret_scanner: 自定义扫描器(默认全局单例)
            merge_threshold: 合并阈值(0-1)
        """
        self.embed_fn = embed_fn
        self.secret_scanner = secret_scanner or get_default_scanner()
        self.merge_threshold = merge_threshold

    def process(
        self,
        candidates: list[ExtractionCandidate],
        stats: Optional[ExtractStats] = None,
    ) -> list[ExtractionCandidate]:
        """
        主入口: 校验 → 过滤 secret → 合并 → 返回

        Args:
            candidates: 原始 LLM 提取结果
            stats: 可选, 接收处理统计

        Returns:
            处理后的 candidates (可直接 write)
        """
        if stats is None:
            stats = ExtractStats()
        stats.input_count = len(candidates)

        # 1. 校验 + 过滤 secret
        valid: list[ExtractionCandidate] = []
        for c in candidates:
            try:
                self._validate_candidate(c)
            except CandidateRejected as e:
                reason = str(e)
                stats.rejection_reasons[reason] = stats.rejection_reasons.get(reason, 0) + 1
                stats.rejected_count += 1
                continue
            if self._has_secret(c):
                stats.secret_filtered += 1
                stats.rejected_count += 1
                stats.rejection_reasons["secret_detected"] = stats.rejection_reasons.get("secret_detected", 0) + 1
                logger.warning(
                    f"extractor 过滤含密钥 candidate: {c.title!r}"
                )
                continue
            valid.append(c)

        # 2. 合并(L1)
        merged = self._merge(valid)
        stats.merged_count = len(valid) - len(merged)

        # 3. 截断(防止 LLM 一次输出 100 条灌爆)
        #    留 50 条,够系统消费
        if len(merged) > 50:
            logger.warning(f"extractor 截断: {len(merged)} → 50")
            merged = merged[:50]

        stats.accepted_count = len(merged)
        logger.info(stats.summary())
        return merged

    # ── 校验 ────────────────────────────────────────────

    def _validate_candidate(self, c: ExtractionCandidate) -> None:
        """
        L7 + 类型校验

        Raises:
            CandidateRejected: 字段缺失 / 类型无效
        """
        # type 必填且合法
        if not c.type:
            raise CandidateRejected("type 字段缺失")
        try:
            validate_type(c.type)
        except ValueError as e:
            raise CandidateRejected(f"type 非法: {e}")

        # title 必填
        if not c.title or not c.title.strip():
            raise CandidateRejected("title 字段缺失或空")

        # body 必填
        if not c.body or not c.body.strip():
            raise CandidateRejected("body 字段缺失或空")

        # L7: source_quote 必填
        if not c.source_quote or not c.source_quote.strip():
            raise CandidateRejected("source_quote 缺失(L7 不变量)")

        # body 长度上限(防止 LLM 灌超长内容)
        if len(c.body) > 5000:
            raise CandidateRejected(f"body 过长({len(c.body)} > 5000)")

    def _has_secret(self, c: ExtractionCandidate) -> bool:
        """L8: 检查 candidate 是否含密钥"""
        full = f"{c.title}\n{c.body}\n{c.source_quote}"
        result = self.secret_scanner.scan(full)
        return not result.is_clean

    # ── 合并 ────────────────────────────────────────────

    def _merge(
        self, candidates: list[ExtractionCandidate]
    ) -> list[ExtractionCandidate]:
        """
        L1 合并: 同 type 且高相似度 → 留最长 body

        复杂度: O(n²),n=50 上限可接受
        """
        if len(candidates) <= 1:
            return list(candidates)

        # 1. 按 type 分桶(只合并同 type)
        by_type: dict[str, list[ExtractionCandidate]] = {}
        for c in candidates:
            by_type.setdefault(c.type, []).append(c)

        # 2. 每桶内合并
        result: list[ExtractionCandidate] = []
        for type_, group in by_type.items():
            result.extend(self._merge_group(group))

        return result

    def _merge_group(
        self, group: list[ExtractionCandidate]
    ) -> list[ExtractionCandidate]:
        """单 type 组内合并"""
        kept: list[ExtractionCandidate] = []
        # 按 body 长度降序 → 优先保留最长的
        sorted_g = sorted(group, key=lambda c: len(c.body), reverse=True)

        for c in sorted_g:
            is_dup = False
            c_norm = _normalize_text(c.body)
            for k in kept:
                k_norm = _normalize_text(k.body)
                sim = self._similarity(c_norm, k_norm)
                if sim >= self.merge_threshold:
                    # 是重复,跳过
                    is_dup = True
                    break
            if not is_dup:
                kept.append(c)
        return kept

    def _similarity(self, a_norm: str, b_norm: str) -> float:
        """
        相似度计算:
        - 有 embed_fn → cos similarity
        - 否则 → max(jaccard, containment)
        """
        if self.embed_fn is not None:
            try:
                va = self.embed_fn.encode(a_norm)
                vb = self.embed_fn.encode(b_norm)
                return self._cos(va, vb)
            except Exception:
                # embed 失败降级 jaccard
                pass
        return _similarity_combined(a_norm, b_norm)

    @staticmethod
    def _cos(a: list[float], b: list[float]) -> float:
        """cosine similarity"""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)


__all__ = [
    "MemoryExtractor",
    "ExtractStats",
    "ExtractorError",
    "CandidateRejected",
]
