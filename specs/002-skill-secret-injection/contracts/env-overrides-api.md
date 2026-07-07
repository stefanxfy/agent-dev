# Contract: env_overrides Public API

**Date**: 2026-07-06
**Status**: Stable for v1
**Related**: [data-model.md](../data-model.md), [spec.md](../spec.md) §FR-007..FR-012

## Purpose

Defines the public Python API of the `agent_core.skills.env_overrides` module. Consumers of this API are:
- `agent_core/turn_chain.py` (the `SkillsPromptHandler` integration)
- Test files
- Future tooling (e.g., a CLI command to test secret resolution)

## Module: `agent_core.skills.env_overrides`

### Public Symbols

```python
# agent_core/skills/env_overrides/__init__.py or re-exports via barrel

from agent_core.skills.env_overrides import (
    SecretRefKind,        # enum
    SecretRef,            # frozen dataclass
    SecretResolutionError,  # exception class
    resolve_secret,       # function: SecretRef -> str
    apply_skill_env_overrides,  # function: (entries, config) -> Callable[[], None]
)
```

### `SecretRefKind`

```python
class SecretRefKind(str, Enum):
    INLINE = "inline"
    ENV = "env"
    FILE = "file"
    SECRET_REF = "secret_ref"  # reserved for v1.1
```

### `SecretRef`

```python
@dataclass(frozen=True)
class SecretRef:
    kind: SecretRefKind
    value: str
```

Constructor parameters: `kind` (required), `value` (required, non-empty).

### `SecretResolutionError`

```python
class SecretResolutionError(SkillError):
    """Raised when a SecretRef cannot be resolved at run time.

    Examples:
    - ENV kind but the env var is not set
    - FILE kind but the file does not exist or is unreadable
    - SECRET_REF kind (v1: not implemented)

    NOTE: This exception is caught and logged as a warning inside
    apply_skill_env_overrides. It is NOT raised to the caller.
    Callers that want fail-fast semantics should call resolve_secret()
    directly (which DOES raise this exception).
    """
```

### `resolve_secret(ref: SecretRef) -> str`

Resolves a `SecretRef` to its actual string value at run time.

**Parameters**:
- `ref`: the `SecretRef` to resolve

**Returns**: the resolved secret string

**Raises**:
- `SecretResolutionError`:
  - If `ref.kind == ENV` and `os.environ[ref.value]` is not set
  - If `ref.kind == FILE` and the file does not exist or is unreadable
- `NotImplementedError`:
  - If `ref.kind == SECRET_REF` (reserved for v1.1 vault integration)

**Side effects**:
- For `FILE` kind: reads the file from disk (one read per call, no caching)
- No other side effects; pure function otherwise

**Example**:
```python
ref = SecretRef(kind=SecretRefKind.INLINE, value="abc123")
assert resolve_secret(ref) == "abc123"

ref_env = SecretRef(kind=SecretRefKind.ENV, value="HOME")
assert resolve_secret(ref_env) == os.environ["HOME"]

ref_file = SecretRef(kind=SecretRefKind.FILE, value="/tmp/secret.txt")
(Path("/tmp/secret.txt")).write_text("file_value\n")
assert resolve_secret(ref_file) == "file_value"  # whitespace stripped
```

### `apply_skill_env_overrides(entries, config) -> Callable[[], None]`

Injects configured secrets into `os.environ` and returns a reverter function. The reverter restores the original state of `os.environ` when called.

**Parameters**:
- `entries` (`Sequence[SkillEntry]`): the loaded skill entries (typically from `SkillsRegistry.snapshot().entries`)
- `config` (`SkillsConfig`): the skill config, must have `config.entries` populated with `SkillEntryConfig` mappings

**Returns**: a reverter function with signature `() -> None`. Calling it restores the pre-injection state of `os.environ`.

**Side effects**:
- Reads `config.entries` to determine which secrets to inject
- Reads `entry.metadata.requires.env` to validate secret names (defense in depth)
- Reads `os.environ` (for ENV refs) and reads files (for FILE refs)
- Mutates `os.environ` by setting values for injected keys

**Raises**:
- No exceptions are raised from this function. Errors during secret resolution are caught, logged as warnings, and the corresponding key is not injected. The function returns a reverter that can be safely called even if no keys were injected (no-op).

**Behavior**:
1. For each `entry` in `entries`:
   - Skip if `entry.metadata` is `None` (broken skill, no requires)
   - Skip if `entry.metadata.requires` is `None`
   - Skip if `entry.metadata.requires.env` is empty
   - Look up `config.entries.get(entry.skill.name)`; skip if absent
2. For each `secret_name, secret_ref` in `entry_cfg.secrets.items()`:
   - If `secret_name` is not in `entry.metadata.requires.env`, skip (defense in depth; validation should have caught this)
   - Try to `resolve_secret(secret_ref)`. On failure, log warning and continue.
   - If `secret_name` not yet seen: snapshot `os.environ.get(secret_name)` into the reverter dict (value may be `None`)
   - Set `os.environ[secret_name] = resolved_value`
3. Return a reverter function that restores each snapshotted key to its previous value (or pops if the previous value was `None`)

**Example**:
```python
# Setup
from agent_core.skills.config import SkillsConfig, SkillEntryConfig, SecretRef, SecretRefKind

config = SkillsConfig(
    paths=...,
    limits=...,
    load=...,
    entries={
        "github-pr-review": SkillEntryConfig(
            secrets={"GITHUB_TOKEN": SecretRef(SecretRefKind.INLINE, "ghp_xxx")}
        )
    }
)

# Pre-state
assert "GITHUB_TOKEN" not in os.environ

# Inject
entries = registry.snapshot().entries  # sequence of SkillEntry
reverter = apply_skill_env_overrides(entries, config)
assert os.environ["GITHUB_TOKEN"] == "ghp_xxx"

# Restore
reverter()
assert "GITHUB_TOKEN" not in os.environ  # restored to pre-state
```

## Thread Safety

**v1 assumption (per spec A-002)**: A single agent run is single-threaded. The reverter is not thread-safe; if a second thread mutates `os.environ` between `apply_skill_env_overrides` and the reverter, behavior is undefined.

Future v2 work may add thread-local reverter state.

## Versioning

This API is **v1**. Additions (e.g., new `SecretRefKind` variants) will be backward compatible (existing callers continue to work). Breaking changes require a major version bump.
