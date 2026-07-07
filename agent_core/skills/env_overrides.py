"""
Skill Secret Injection — 运行时环境变量注入与还原(env_overrides UseCase 层)

设计来源:specs/002-skill-secret-injection(contract: contracts/env-overrides-api.md)

职责:
- 暴露 `SecretRef` / `SkillEntryConfig` 解析后的实际值
- 在 agent run 入口(per-turn inputs_chain 起始)把 secret 注入 os.environ
- 返回 reverter 函数(snapshot-on-first + try/finally 还原)

公开 API:
- SkillError               (基类; SecretResolutionError / ConfigValidationError 的父类)
- SecretResolutionError    (运行时解析失败 — apply 路径仅 warn, 不 raise)
- ConfigValidationError    (加载期校验失败 — Registry 抛, fail-fast at startup)
- SecretRefKind            (str-Enum: INLINE / ENV / FILE / SECRET_REF)
- SecretRef                (frozen dataclass; __post_init__ 校验 value 非空 + FILE 绝对路径)
- SkillEntryConfig         (frozen dataclass; enabled + secrets 映射)
- resolve_secret(ref)      (SecretRef → str; ENV/FILE/US3 由 US3 T018/T019 真实现)
- apply_skill_env_overrides(entries, config) -> Callable[[], None]
                            (inject + snapshot + 返 reverter)
- load_user_config(default) -> SkillsConfig
                            (T033: AGENT_CONFIG_PATH / 默认 ~/.agent_data/config.yaml 加载;
                             把 entries 字段 overlay 到 default)

非职责:
- 不调 LLM / 不读 session.jsonl
- 不做 secret 持久化(FR-015: secret 值不入 session)
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional


# ──────────────────────────────────────────────────────────────────
# 异常族(置于本模块,供 config.py / registry.py / turn_chain.py 复用)
# ──────────────────────────────────────────────────────────────────
# 设计:SkillError 作为整个 skill 子系统的根异常;SecretResolutionError +
# ConfigValidationError 是其子类,各自表达"运行期" vs "加载期"的失败语义。
# 调用方 catch 根类 SkillError 即可捕获全部 skill 错误(向后兼容)。
#
# 后续如新增 SkillTypeError / SkillNotFoundError 等,亦挂此根下。

class SkillError(Exception):
    """skill 子系统根异常(所有 skill 相关异常的基类)。"""


class SecretResolutionError(SkillError):
    """运行时 secret 解析失败(US1 inline 不会抛;US3 env/file 抛)。

    apply_skill_env_overrides 捕获本异常并 log warning, 不向 caller 抛
    (degrade gracefully; 工具自己会报错)。直接调 resolve_secret() 的
    caller 可拿到本异常(fail-fast 语义)。
    """


class ConfigValidationError(SkillError):
    """加载期配置校验失败(unknown skill name / secret name ⊄ requires.env)。

    SkillsRegistry.__init__ 一次性聚合所有错误后 raise, fail-fast at startup。
    """


# ──────────────────────────────────────────────────────────────────
# SecretRef 形态(INLINE / ENV / FILE / SECRET_REF)
# ──────────────────────────────────────────────────────────────────
# SECRET_REF 保留为 v1.1 vault 集成入口,当前 resolve_secret 会 raise
# NotImplementedError("SECRET_REF reserved for v1.1")。
# 字段定义放在 config.py(T002);此处 re-export 占位(T001 skeleton 阶段)。


class SecretRefKind(str, Enum):
    """SecretRef.kind 枚举值(str 子类以利 YAML 序列化)。"""
    INLINE = "inline"
    ENV = "env"
    FILE = "file"
    SECRET_REF = "secret_ref"  # reserved for v1.1


@dataclass(frozen=True)
class SecretRef:
    """运行时 secret 解析形态(frozen,immutable)。

    字段:
    - kind: SecretRefKind 枚举值(INLINE / ENV / FILE / SECRET_REF)
    - value: 字面值 / env var 名 / 文件绝对路径

    验证(__post_init__, data-model §Validation Rules 表):
    - value MUST 非空字符串(空 → raise ValueError)
    - kind=FILE → value MUST startswith("/")(绝对路径, 避免与相对路径歧义)
    """
    kind: SecretRefKind
    value: str

    def __post_init__(self) -> None:
        # pydantic 风格 validator 在 frozen dataclass 上以 __post_init__ 表达
        # (T002 设计决策; 镜像 pydantic min_length=1 + FILE absolute path 校验)
        if not isinstance(self.value, str):
            raise ValueError(
                f"SecretRef.value must be str, got {type(self.value).__name__}"
            )
        if len(self.value) < 1:
            raise ValueError("SecretRef.value must be non-empty (min_length=1)")
        if self.kind == SecretRefKind.FILE and not self.value.startswith("/"):
            raise ValueError(
                f"SecretRef.kind=FILE requires absolute path (startswith '/'), got {self.value!r}"
            )


@dataclass(frozen=True)
class SkillEntryConfig:
    """单个 skill 的用户配置(Value Object,frozen)。

    字段(data-model §SkillEntryConfig):
    - enabled: bool = True
        - 是否启用(v1: 永远视为 True,保留为 v1.1 UI 关闭单个 skill 留口)
    - secrets: Mapping[str, SecretRef] = {}
        - env var 名 → SecretRef 解析形态

    验证(SkillsRegistry.__init__ T005 调 validate_entries_against_skills):
    - secrets 的每个 key MUST ⊆ 对应 skill 的 metadata.requires.env
    """
    enabled: bool = True
    secrets: Mapping[str, SecretRef] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────
# 模块 logger(debug 前缀 🧩 复用 logging_setup 子 logger 机制)
# ──────────────────────────────────────────────────────────────────
# 2026-07-06:核心环节 MUST 打 debug 日志,复用 logging_setup 子 logger 机制
# (🧩 agent_core.skills.env_overrides),便于测试追溯与 bug 定位。
# 严禁在日志中记 secret value(FR-014); key 名可记。

_logger = logging.getLogger("agent_core.skills.env_overrides")


# ──────────────────────────────────────────────────────────────────
# 公开 API 占位(US1 T007/T008 真实现; 此处仅 docstring + NotImplementedError stub)
# ──────────────────────────────────────────────────────────────────
# intentionally stubbed: 实现在 T007(resolve_secret) + T008(apply_skill_env_overrides);
# 此处先放 stub 以让包 import 不报错, US1 implementation tasks 再覆盖。

def resolve_secret(ref: SecretRef) -> str:  # noqa: D401
    """SecretRef → resolved string。

    Raises:
        SecretResolutionError: ENV/FILE 解析失败(US3 T018/T019 真实现)
        NotImplementedError: SECRET_REF(v1.1 vault 未实现)

    Side effects:
        FILE kind 触发磁盘读取(一次,不缓存)。
    """
    if ref.kind == SecretRefKind.INLINE:
        # US1 T007: 字面值直接返回(不做 strip, 用户显式控制)
        return ref.value
    if ref.kind == SecretRefKind.SECRET_REF:
        # v1.1 vault 集成保留口(Edge Cases "Vault/external secret store")
        raise NotImplementedError(
            "SecretRefKind.SECRET_REF reserved for v1.1 vault integration"
        )
    # ──────────────────────────────────────────────────────────────────
    # US3 T018: ENV form — 读 os.environ[ref.value], 不存在抛 SecretResolutionError
    # ──────────────────────────────────────────────────────────────────
    # 设计决策(spec FR-004 case 2 + quickstart §3):
    # - 不存在 raise SecretResolutionError — apply_skill_env_overrides 捕获后 warn+skip
    # - 返回值不做 strip(与 shell `env` 行为一致 — 保留字面空白)
    if ref.kind == SecretRefKind.ENV:
        value = os.environ.get(ref.value)
        if value is None:
            raise SecretResolutionError(
                f"env var {ref.value!r} not set"
            )
        return value
    # ──────────────────────────────────────────────────────────────────
    # US3 T019: FILE form — 读 Path(ref.value) 文件内容, 不存在/不可读抛 SecretResolutionError
    # ──────────────────────────────────────────────────────────────────
    # 设计决策(spec FR-004 case 3 + quickstart §3):
    # - Path(ref.value).read_text() — SecretRef.__post_init__ 已强制 startswith('/')
    # - 文件不存在/不可读 → 抛 SecretResolutionError(apply 捕获后 warn+skip, FR-012)
    # - 返回值 .strip() 移除首尾空白(文件内容通常带 trailing newline, 显式剔除)
    if ref.kind == SecretRefKind.FILE:
        path = Path(ref.value)
        try:
            content = path.read_text()
        except (OSError, IOError) as e:
            # FileNotFoundError / PermissionError / IsADirectoryError 等
            raise SecretResolutionError(
                f"file {ref.value!r} unreadable: {e}"
            ) from e
        return content.strip()
    # 未识别 kind(理论上 dataclass + Enum 不会到这里; 防御兜底)
    raise SecretResolutionError(f"未知 SecretRefKind: {ref.kind!r}")


def apply_skill_env_overrides(entries, config):  # noqa: D401
    """注入 secrets 到 os.environ + 返 reverter(snapshot-on-first)。

    实现细节:
    - 遍历 entries, 每个 entry 读 metadata.requires.env + config.entries[skill_name].secrets
    - snapshot-on-first: 已见 key 不重复 snapshot(US2 T015 多 skill 共享语义)
    - try-except per key: 单 key 解析失败 log warning + skip, 不阻断其他 key
    - 返 Callable[[], None]: 还原 os.environ 到 snapshot-on-first 时的状态

    Args:
        entries: Sequence[SkillEntry](来自 registry.snapshot().entries)
        config: SkillsConfig(读 config.entries 字段; None 或 entries=None 视为无注入)

    Returns:
        Callable[[], None] — reverter 函数。 空注入时也是 no-op safe reverter。

    Raises:
        不抛任何异常(降级路径在内部 try-except; 防御式务实工程)。
    """
    # 防御: 缺 entries 字段 → no-op reverter
    cfg_entries = getattr(config, "entries", None)
    if not cfg_entries:
        def _noop_reverter() -> None:
            return None
        return _noop_reverter

    # snapshot-on-first: 记录每个被注入 key 的原值(None 表示原本不存在)
    injected: dict[str, str | None] = {}
    injected_count = 0

    for entry in entries or []:
        # entry 可能 metadata=None (broken skill) 或 requires 为空
        metadata = getattr(entry, "metadata", None)
        if metadata is None or metadata.requires is None:
            continue
        required_env = metadata.requires.env or ()
        if not required_env:
            continue

        # 取 config 里对应 skill 的 entries
        skill_name = entry.skill.name
        entry_cfg = cfg_entries.get(skill_name)
        if entry_cfg is None:
            continue

        for secret_name, secret_ref in entry_cfg.secrets.items():
            # 防御: secret_name 不在 requires.env → 跳过(校验应已抓但 runtime 兜底)
            if secret_name not in required_env:
                _logger.warning(
                    "🧩 skip inject: skill=%s secret=%s (not in requires.env=%s)",
                    skill_name, secret_name, list(required_env),
                )
                continue

            # try resolve, 单 key 失败不阻断其他 key
            try:
                value = resolve_secret(secret_ref)
            except Exception as e:
                # SECRET_REF 抛 NotImplementedError; US3 stub 抛 NotImplementedError;
                # 实际 missing env/file 抛 SecretResolutionError. 全部降级为 warn.
                if isinstance(e, NotImplementedError):
                    _logger.warning(
                        "🧩 skip inject: skill=%s secret=%s kind=%s — %s",
                        skill_name, secret_name, secret_ref.kind.value,
                        "SECRET_REF not implemented v1.1" if secret_ref.kind == SecretRefKind.SECRET_REF
                        else f"{secret_ref.kind.value} form not yet implemented",
                    )
                else:
                    _logger.warning(
                        "🧩 skip inject: skill=%s secret=%s kind=%s — resolve failed: %s",
                        skill_name, secret_name, secret_ref.kind.value, e,
                    )
                continue

            # snapshot-on-first: 已见 key 不重复 snapshot
            if secret_name not in injected:
                injected[secret_name] = os.environ.get(secret_name)
            os.environ[secret_name] = value
            injected_count += 1
            _logger.debug(
                "🧩 injected: skill=%s secret=%s kind=%s",
                skill_name, secret_name, secret_ref.kind.value,
            )

    def _reverter() -> None:
        """还原 os.environ 到 snapshot-on-first 时的状态。

        - 原值非 None → 还原
        - 原值 None → pop(从未设过)
        """
        for key, prev in injected.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        _logger.debug("🧩 env reverted: keys=%d", len(injected))

    # 把 reverter + injected_count 装到闭包, 供 EnvCleanupHandler 等消费者 inspect
    setattr(_reverter, "_injected_keys", list(injected.keys()))
    setattr(_reverter, "_injected_count", injected_count)
    return _reverter


# ──────────────────────────────────────────────────────────────────
# load_user_config(T033)— 用户级 config 文件加载(env 路径优先 + 默认 fallback)
# ──────────────────────────────────────────────────────────────────
# 设计要点:
# - 优先读 env AGENT_CONFIG_PATH(若设, expanduser + 校验 isfile; 缺失 raise)
# - fallback: ~/.agent_data/config.yaml(expanduser)
# - 文件不存在 → 返 default(向后兼容, 不报错)
# - 文件存在 → SkillsConfig.from_yaml(path), 把 entries 字段 overlay 到 default.entries
# - chmod != 600 → log warning 不 fail(FR-017: warn-not-fail, OS-dependent)
#
# 关键不变量:
# - default 是 SkillsConfig 或 None; None 时返 None(让 caller 决定如何 fallback)
# - 默认情况下, default 不传 None 时(default.entries=None 视为 legacy)
#   文件若有 entries → 赋给结果; 文件若无 entries → 保留 default.entries(通常 None)
#
# 用法(T038): 在 ReactAgent.__init__ 中, 在 SkillsRegistry 构造之前调
#     config = load_user_config(skills_config)
#     self.skills_registry = SkillsRegistry(config)

_DEFAULT_CONFIG_PATH = Path.home() / ".agent_data" / "config.yaml"


def _default_config_path() -> Path:
    """每次调用计算默认 config 路径(Path.home() 在测试场景下会被 patch)。"""
    return Path.home() / ".agent_data" / "config.yaml"


def _check_config_file_permissions(path: Path) -> None:
    """FR-017: chmod != 600 → log warning, 不 fail(防御式务实工程)。

    Windows: st_mode 不含 group/other bits → no-op(不同 OS 模型)。
    """
    try:
        st = path.stat()
    except OSError as e:
        _logger.debug(f"无法 stat config 文件 {path}: {e}")
        return
    mode = st.st_mode
    # group/other 读位检查(0o077)
    if mode & stat.S_IRWXG or mode & stat.S_IRWXO:
        _logger.warning(
            "⚠️ [Config Perm] %s 不是 chmod 600(当前 mode=%o); "
            "建议 chmod 600 ~/.agent_data/config.yaml 保护 secret 不被同机其他用户读",
            path,
            stat.S_IMODE(mode),
        )


def load_user_config(default: Optional["SkillsConfig"] = None) -> Optional["SkillsConfig"]:
    """从 AGENT_CONFIG_PATH(env 优先) 或 ~/.agent_data/config.yaml(default) 加载。

    Returns:
        SkillsConfig(可能与 default 是同一实例或新构造):
        - default 为 None → 返 None(让 caller 决定)
        - 文件不存在 → 返 default(向后兼容, 不报错)
        - 文件存在 → 调 SkillsConfig.from_yaml(path), 把其 entries 字段 overlay 到 default
        - 文件存在但缺 entries 段 → 保留 default.entries(通常 None)

    Raises:
        ConfigValidationError: AGENT_CONFIG_PATH 指向不存在的文件 或 YAML 解析失败
    """
    # Lazy import:避免 env_overrides ↔ config 互相 import 死锁
    from agent_core.skills.config import SkillsConfig

    if default is None:
        return None

    # 1. 决定 path(env 优先 → 否则 default path)
    env_path = os.environ.get("AGENT_CONFIG_PATH")
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_file():
            raise ConfigValidationError(
                f"AGENT_CONFIG_PATH={env_path} 指向的文件不存在或不是普通文件"
            )
        source = "env"
    else:
        path = _default_config_path()
        if not path.is_file():
            # 默认文件不存在 → legacy 模式, 返 default
            _logger.debug("🧩 user config 文件不存在: %s (fallback to default)", path)
            return default
        source = "default"

    # 2. 权限检查(FR-017: warn-not-fail)
    _check_config_file_permissions(path)

    # 3. 读 YAML 解析
    try:
        loaded = SkillsConfig.from_yaml(path)
    except ConfigValidationError:
        # from_yaml 已抛 ConfigValidationError; 透传
        raise

    # 4. Overlay: loaded.entries 优先; 若 None / 空则保留 default.entries
    if loaded.entries:
        default.entries = loaded.entries
        _logger.info(
            "🧩 user config loaded: entries=%d source=%s path=%s",
            len(loaded.entries), source, path,
        )
    else:
        _logger.debug(
            "🧩 user config loaded (no entries): source=%s path=%s",
            source, path,
        )
    return default


__all__ = [
    "SkillError",
    "SecretResolutionError",
    "ConfigValidationError",
    "SecretRefKind",
    "SecretRef",
    "SkillEntryConfig",
    "resolve_secret",
    "apply_skill_env_overrides",
    "load_user_config",
]