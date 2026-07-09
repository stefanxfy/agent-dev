# Data Model: Skill Secret Injection

**Phase**: 1 (Design & Contracts)
**Date**: 2026-07-06

This document specifies the data entities introduced by the Skill Secret Injection feature. It builds on the existing data model in [`specs/001-skill-system/data-model.md`](../001-skill-system/data-model.md).

## Entity Diagram

```
┌──────────────────────────────────────────────┐
│ SkillsConfig (existing, MODIFIED)            │
│   + entries: Mapping[str, SkillEntryConfig]? │  ← NEW FIELD
└─────────────────┬────────────────────────────┘
                  │ 1:N
                  ▼
┌──────────────────────────────────────────────┐
│ SkillEntryConfig (NEW)                       │
│   - enabled: bool = True                     │
│   - secrets: Mapping[str, SecretRef] = {}   │
└─────────────────┬────────────────────────────┘
                  │ 1:N
                  ▼
┌──────────────────────────────────────────────┐
│ SecretRef (NEW)                              │
│   - kind: SecretRefKind                      │
│   - value: str                               │
└──────────────────────────────────────────────┘
                  │
                  │ referenced by
                  ▼
┌──────────────────────────────────────────────┐
│ SkillEntry (existing)                        │
│   - skill: Skill                             │
│   - metadata: SkillMetadata | None           │
│     - requires: SkillRequires                │
│       - env: tuple[str, ...]                 │  ← existing
└──────────────────────────────────────────────┘
```

## Entities

### SecretRefKind (NEW)

**Purpose**: Enumeration of the ways a secret value can be resolved at run time.

| Variant | String Value | Meaning |
|---|---|---|
| `INLINE` | `"inline"` | The `value` field IS the secret value (plaintext) |
| `ENV` | `"env"` | The `value` field is the name of an env var in `process.env`; read at run time |
| `FILE` | `"file"` | The `value` field is an absolute path; read file contents (stripped) at run time |
| `SECRET_REF` | `"secret_ref"` | RESERVED for v1.1 vault integration. v1 raises `NotImplementedError` if used. |

**Validation**:
- `value` MUST be a non-empty string.
- For `FILE`, `value` MUST start with `/` (absolute path) to avoid ambiguity with relative paths.

### SecretRef (NEW)

**Purpose**: Represents a single secret value and how to resolve it.

```python
@dataclass(frozen=True)
class SecretRef:
    kind: SecretRefKind
    value: str
```

**Fields**:
- `kind`: how to interpret `value` (see `SecretRefKind`)
- `value`: the literal value, env var name, or file path

**Relationships**:
- A `SecretRef` is contained in a `SkillEntryConfig.secrets` map, keyed by env var name.
- The key (env var name) MUST appear in the corresponding `SkillEntry.metadata.requires.env` (enforced at config load by `validate_entries_against_skills`).

**Examples**:
```python
SecretRef(kind=SecretRefKind.INLINE, value="ghp_abc123")     # inline
SecretRef(kind=SecretRefKind.ENV,    value="RAW_GH_TOKEN")   # env ref
SecretRef(kind=SecretRefKind.FILE,   value="/etc/gh_token")   # file ref
```

### SkillEntryConfig (NEW)

**Purpose**: User-side configuration for a single skill. Maps env var names to their `SecretRef`s.

```python
@dataclass(frozen=True)
class SkillEntryConfig:
    enabled: bool = True
    secrets: Mapping[str, SecretRef] = field(default_factory=dict)
```

**Fields**:
- `enabled`: whether the skill is active. Default `True`. Reserved for future use (v1.1: per-skill enable/disable in UI). Not validated against anything in v1; always treated as `True`.
- `secrets`: map of env var name → `SecretRef`. Empty by default.

**Validation** (at config load time, in `SkillsRegistry.__init__`):
- Every key in `secrets` MUST be a subset of the corresponding skill's `metadata.requires.env`. Otherwise, raise `ConfigValidationError` listing the offending keys.

**Examples**:
```python
SkillEntryConfig()                                                        # no secrets (legacy behavior)
SkillEntryConfig(secrets={"GITHUB_TOKEN": SecretRef(INLINE, "ghp_xxx")})  # one secret
SkillEntryConfig(                                                         # multi-secret
    secrets={
        "GITHUB_TOKEN": SecretRef(INLINE, "ghp_xxx"),
        "SLACK_BOT_TOKEN": SecretRef(ENV, "SLACK_RAW"),
    }
)
```

### SkillsConfig.entries (NEW FIELD on existing SkillsConfig)

**Purpose**: Optional per-skill user configuration. Default `None` for backward compatibility.

```python
class SkillsConfig(BaseModel):
    paths: SkillsPathsConfig
    limits: SkillsLimitsConfig
    load: SkillsLoadConfig
    sources: Optional[SourcesConfig] = None
    entries: Optional[Mapping[str, SkillEntryConfig]] = None   # NEW
```

**Semantics**:
- `None` (default): legacy behavior — no secret injection. The system operates exactly as `001-skill-system` does.
- `{}` (empty dict): user has set up the config file but provided no entries. Same as `None` for runtime behavior; the empty dict exists to make the intent explicit.
- Non-empty: per-skill config; each key MUST be a loaded skill name (validated at `SkillsRegistry.__init__`).

### EnvOverrideSnapshot (INTERNAL, NEW)

**Purpose**: Internal state held by the reverter to restore `process.env` after a run. Not a public entity.

```python
@dataclass
class EnvOverrideSnapshot:
    injected_keys: dict[str, str | None]   # env var name → previous value (None if absent)
```

**Lifecycle**:
1. Created at the start of `apply_skill_env_overrides` (populated as secrets are injected).
2. Closed (restored) by the returned reverter function in a `try/finally` block.
3. Not exposed to the user; never serialized.

## State Transitions

`SecretRef` and `SkillEntryConfig` are immutable (`frozen=True` dataclasses). No state transitions.

`EnvOverrideSnapshot` is created and destroyed within a single `apply_skill_env_overrides` call. Its lifecycle is:
```
[caller invokes apply] → [build snapshot + inject] → [return reverter]
                                                            ↓
                                                       [caller's try/finally]
                                                            ↓
                                                       [reverter: restore]
                                                            ↓
                                                       [snapshot discarded]
```

## Validation Rules (Summary)

| Entity.Field | Rule | Enforced Where | Error |
|---|---|---|---|
| `SkillsConfig.entries[*]` | Key MUST be a loaded skill name | `SkillsRegistry.__init__` → `_validate_entries_against_skills` | `ConfigValidationError` |
| `SkillEntryConfig.secrets[*]` | Key MUST be ⊆ corresponding skill's `requires.env` | Same as above | Same |
| `SecretRef.value` | Non-empty string | pydantic `min_length=1` validator | `ValidationError` |
| `SecretRef.kind=FILE` | `value` MUST start with `/` | pydantic validator | `ValidationError` |
| `SecretRef.kind=SECRET_REF` | RESERVED; v1 raises | `resolve_secret` runtime | `NotImplementedError` |
| `SecretRef.kind=ENV` | `value` MUST exist in `os.environ` at run time | `resolve_secret` runtime | `SecretResolutionError` (warning, not fail-fast) |
| `SecretRef.kind=FILE` | File MUST exist and be readable at run time | `resolve_secret` runtime | `SecretResolutionError` (warning, not fail-fast) |
| Config file mode | Warn if readable by group/other | `_check_config_file_permissions` at load | `UserWarning` log |

## Relationships to Existing Entities

This feature does not modify any existing entity. It adds:
- 1 new field to `SkillsConfig` (`entries`)
- 2 new entities (`SecretRef`, `SkillEntryConfig`) and 1 enum (`SecretRefKind`)
- 1 internal entity (`EnvOverrideSnapshot`)

All existing data contracts from `001-skill-system/data-model.md` remain unchanged.

## Deviations from Spec

None at this stage. All entities match the spec's Key Entities section.

If during implementation we discover a need to add fields (e.g., `SecretRef.metadata` for rotation hints), it will be documented here with justification.
