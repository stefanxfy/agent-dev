"""
记忆系统类型定义

M1 / Day 1 — O8 修复 + v2.1 §5.3.1 封闭分类

设计要点：
1. 4 类封闭（user / feedback / project / reference）—— LLM 不能发明第 5 类
2. frontmatter schema 编译期硬约束，违反直接 ValueError
3. feedback / project 必须含 **Why:** —— 防止"只有规则没有原因"的浅记忆
4. 所有校验为纯函数（不依赖外部状态），便于测试 + 跨进程复用
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, TypedDict


# ──────────────────────────────────────────────────────────────────
# 1. 4 类封闭枚举（编译期硬约束）
# ──────────────────────────────────────────────────────────────────

# Literal 是 Python 编译期类型，IDE / mypy / Pydantic 都会拒绝第 5 类
MemoryType = Literal["user", "feedback", "project", "reference"]

# 运行时校验集合（用于外部输入，如 LLM 输出、YAML 解析、API 接收）
_VALID_TYPES: frozenset[str] = frozenset({"user", "feedback", "project", "reference"})

# 4 类语义注释（给 LLM prompt 用的提示，每个类不允许混淆）
_TYPE_DESCRIPTIONS: dict[str, str] = {
    "user":     "用户画像（角色、偏好、身份）—— 关于'我是谁'",
    "feedback": "纠偏习惯（用户反复纠正过的做法）—— 关于'别再这样做'",
    "project":  "项目背景（项目结构、约束、决策）—— 关于'这个项目怎么组织'",
    "reference":"外部指针（文档路径、URL、命令别名）—— 关于'去哪查'",
}


def validate_type(value: Any) -> MemoryType:
    """
    运行时校验记忆类型

    Args:
        value: 待校验值（str / bytes / 其他）

    Returns:
        合法的 MemoryType 字面量

    Raises:
        ValueError: 当 value 不是 4 类之一

    Examples:
        >>> validate_type("user")
        'user'
        >>> validate_type("episodic")  # LLM 试图发明的第 5 类
        Traceback (most recent call last):
            ...
        ValueError: 非法记忆类型 'episodic'，必须为 user/feedback/project/reference 之一
    """
    if not isinstance(value, str):
        raise ValueError(
            f"记忆类型必须是字符串，实际为 {type(value).__name__}"
        )
    if value not in _VALID_TYPES:
        raise ValueError(
            f"非法记忆类型 {value!r}，必须为 "
            f"{'/'.join(sorted(_VALID_TYPES))} 之一"
        )
    return value  # type: ignore[return-value]


def all_types() -> list[MemoryType]:
    """返回所有合法类型（用于 LLM prompt / 文档生成）"""
    return ["user", "feedback", "project", "reference"]


def type_description(t: MemoryType) -> str:
    """返回类型的语义描述（给 LLM 看）"""
    return _TYPE_DESCRIPTIONS[t]


# ──────────────────────────────────────────────────────────────────
# 2. frontmatter schema（每条记忆文件的元数据）
# ──────────────────────────────────────────────────────────────────

class Frontmatter(TypedDict, total=False):
    """
    记忆文件 frontmatter 结构（YAML）

    字段：
        type:       必填，4 类之一
        created_at: 必填，ISO 8601 时间戳
        updated_at: 选填，ISO 8601 时间戳
        tags:       选填，字符串列表（用于分类过滤）
        source:     选填，来源（"user_input" / "extracted" / "manual"）
        item_hash:  必填，item 内容哈希（用于幂等去重，A5）
        schema_version: 必填，整数（用于 schema migration，A7）
    """
    type: MemoryType
    created_at: str
    updated_at: str
    tags: list[str]
    source: str
    item_hash: str
    schema_version: int


# 必填字段清单
_REQUIRED_FRONTMATTER: frozenset[str] = frozenset({
    "type", "created_at", "item_hash", "schema_version",
})

# 选填字段清单
_OPTIONAL_FRONTMATTER: frozenset[str] = frozenset({
    "updated_at", "tags", "source",
    "importance",            # M3: 1-10 重要性(MemoryStore 透传,cold_start 用)
    "seed_origin",           # M3: 原始来源标识(区分 user_input/manual/seed)
})

# 哪些类型必须含 **Why:** 段落（v2.1 §4.5 #7 不变量）
_TYPES_REQUIRING_WHY: frozenset[str] = frozenset({"feedback", "project"})

# schema_version 当前值（升级时 +1）
CURRENT_SCHEMA_VERSION: int = 2


def _is_iso8601(s: Any) -> bool:
    """校验 ISO 8601 时间戳（容忍无时区 / 有时区）"""
    if not isinstance(s, str):
        return False
    try:
        # datetime.fromisoformat 在 Python 3.11+ 支持 'Z' 后缀
        # 3.10 及以下需要先去掉 Z
        candidate = s.replace("Z", "+00:00") if s.endswith("Z") else s
        datetime.fromisoformat(candidate)
        return True
    except (ValueError, AttributeError):
        return False


def validate_frontmatter(data: Any) -> Frontmatter:
    """
    校验 frontmatter dict

    Args:
        data: 任意 dict（通常来自 YAML 解析）

    Returns:
        合法的 Frontmatter（TypedDict，运行时当 dict 用）

    Raises:
        ValueError: 字段缺失 / 类型错误 / schema_version 过旧
    """
    if not isinstance(data, dict):
        raise ValueError(
            f"frontmatter 必须是 dict，实际为 {type(data).__name__}"
        )

    # 1. 必填字段检查
    missing = _REQUIRED_FRONTMATTER - set(data.keys())
    if missing:
        raise ValueError(
            f"frontmatter 缺少必填字段: {sorted(missing)}"
        )

    # 2. type 校验
    validate_type(data["type"])

    # 3. created_at / updated_at ISO 8601
    if not _is_iso8601(data["created_at"]):
        raise ValueError(
            f"created_at 必须是 ISO 8601 字符串，实际为 {data['created_at']!r}"
        )
    if "updated_at" in data and not _is_iso8601(data["updated_at"]):
        raise ValueError(
            f"updated_at 必须是 ISO 8601 字符串，实际为 {data['updated_at']!r}"
        )

    # 4. item_hash 必须是 64 字符 hex（SHA-256）
    if not isinstance(data["item_hash"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", data["item_hash"]
    ):
        raise ValueError(
            f"item_hash 必须是 64 字符 hex（SHA-256），实际为 {data['item_hash']!r}"
        )

    # 5. schema_version 检查
    if not isinstance(data["schema_version"], int) or data["schema_version"] < 1:
        raise ValueError(
            f"schema_version 必须是 >=1 的整数，实际为 {data['schema_version']!r}"
        )
    if data["schema_version"] > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version {data['schema_version']} 高于当前支持版本 "
            f"{CURRENT_SCHEMA_VERSION}，请升级 agent-dev"
        )

    # 6. tags 必须是字符串列表
    if "tags" in data:
        if not isinstance(data["tags"], list):
            raise ValueError(
                f"tags 必须是字符串列表，实际为 {type(data['tags']).__name__}"
            )
        for tag in data["tags"]:
            if not isinstance(tag, str) or not tag.strip():
                raise ValueError(
                    f"tags 元素必须是非空字符串，实际为 {tag!r}"
                )

    # 7. source 必须是已知值
    if "source" in data:
        if data["source"] not in {"user_input", "extracted", "manual"}:
            raise ValueError(
                f"source 必须是 user_input/extracted/manual 之一，实际为 {data['source']!r}"
            )

    # 8. 未知字段警告（不阻断，但记录）
    known = _REQUIRED_FRONTMATTER | _OPTIONAL_FRONTMATTER
    unknown = set(data.keys()) - known
    if unknown:
        # 不抛异常，但允许调用方通过捕获日志发现
        import warnings
        warnings.warn(f"frontmatter 含未知字段: {sorted(unknown)}", stacklevel=2)

    return data  # type: ignore[return-value]


# ──────────────────────────────────────────────────────────────────
# 3. body 内容校验（v2.1 §4.5 #7：feedback/project 必须含 **Why:**）
# ──────────────────────────────────────────────────────────────────

_WHY_PATTERN = re.compile(r"\*\*Why:\*\*\s*\S", re.MULTILINE)


def validate_body(type_: MemoryType, body: str) -> None:
    """
    校验记忆文件 body 内容

    v2.1 §4.5 不变量 #7：feedback / project 必须含 **Why:** 段落，
    避免"只有规则没有原因"的浅记忆。

    Args:
        type_: 记忆类型
        body: 文件正文（不含 frontmatter）

    Raises:
        ValueError: 当 type_ 要求 **Why:** 但 body 缺失时
    """
    if type_ in _TYPES_REQUIRING_WHY and not _WHY_PATTERN.search(body):
        raise ValueError(
            f"{type_} 类型记忆必须包含 '**Why:**' 段落（v2.1 §4.5 #7 不变量），"
            f"否则属于'只有规则没有原因'的浅记忆"
        )


__all__ = [
    "MemoryType",
    "Frontmatter",
    "validate_type",
    "all_types",
    "type_description",
    "validate_frontmatter",
    "validate_body",
    "CURRENT_SCHEMA_VERSION",
]