"""
T010 — tests/test_skill_secret_refs.py

002-skill-secret-injection (US1): resolve_secret 单元测试。

覆盖(US1 Phase 3 MVP):
1. INLINE 返字面值
2. SECRET_REF 抛 NotImplementedError(v1.1 vault)

US3 T021 (ENV/FILE 形式) 在 Phase 5 增量添加 — 见 test_skill_secret_refs_us3.py 后续 split。
"""

from __future__ import annotations

import pytest

from agent_core.skills.env_overrides import (
    SecretRef,
    SecretRefKind,
    resolve_secret,
)


def test_resolve_inline_returns_value_verbatim():
    """US1 T007: INLINE kind → 返 ref.value 字面值, 不做变换。"""
    ref = SecretRef(kind=SecretRefKind.INLINE, value="inline_token_abc123")
    assert resolve_secret(ref) == "inline_token_abc123"


def test_resolve_secret_ref_raises_not_implemented():
    """US1 T007: SECRET_REF → raise NotImplementedError(v1.1 vault 保留口)。"""
    ref = SecretRef(kind=SecretRefKind.SECRET_REF, value="vault://any")
    with pytest.raises(NotImplementedError) as exc_info:
        resolve_secret(ref)
    assert "v1.1" in str(exc_info.value)