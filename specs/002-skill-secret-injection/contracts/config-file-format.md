# Contract: User Config File Format

**Date**: 2026-07-06
**Status**: Stable for v1
**Related**: [data-model.md](../data-model.md), [spec.md](../spec.md) §FR-003..FR-006

## Purpose

This contract defines the **user-private configuration file** that maps skill names to their secret values. The file is the user-facing interface for secret injection; everything else in the feature is implementation detail.

## File Location

- **Default**: `~/.agent_data/config.yaml`
- **Override**: environment variable `AGENT_CONFIG_PATH` (absolute path)

The file MUST be created with `chmod 600` (or stricter) permissions. The system will warn (not fail) at load time if the file is readable by group or other.

## File Format: YAML

Top-level structure: a YAML object with a `skills` key, which contains an `entries` map.

```yaml
# ~/.agent_data/config.yaml
# (chmod 600, git-ignored)

skills:
  entries:
    <skill-name-1>:
      enabled: <bool>          # optional, default true
      secrets:
        <ENV_VAR_NAME_1>: <value>
        <ENV_VAR_NAME_2>: <secret-ref-or-string>
        ...
    <skill-name-2>:
      secrets: ...
```

### Field Semantics

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `skills` | object | yes | — | Top-level container |
| `skills.entries` | map of string → object | no | `{}` | Per-skill configurations |
| `skills.entries.<name>.enabled` | bool | no | `true` | Whether the skill is active. (v1: always treated as `true`; reserved for future use.) |
| `skills.entries.<name>.secrets` | map of string → value | no | `{}` | Map of env var name → secret value or reference |

### Secret Value Forms

Each entry in `secrets.<ENV_VAR_NAME>` can be either:

#### Form 1: Inline string (default)

The value is the literal secret.

```yaml
secrets:
  GITHUB_TOKEN: "ghp_abc123def456"
  SLACK_BOT_TOKEN: "xoxb-7890123456"
```

The string `ghp_abc123def456` is taken as-is. No transformation.

#### Form 2: `env://` reference

The value starts with `env://` followed by an environment variable name. At run time, the system reads `os.environ[<name>]` and uses that as the secret value.

```yaml
secrets:
  GITHUB_TOKEN: "env://RAW_GITHUB_TOKEN"
```

Resolution: `os.environ["RAW_GITHUB_TOKEN"]` must be set in the calling shell. If absent, the system logs a warning and does NOT inject the secret (no fail-fast; the tool will fail with its own error when it tries to use the missing value).

#### Form 3: `file://` reference

The value starts with `file://` followed by an absolute path. At run time, the system reads the file contents (stripped of leading/trailing whitespace) and uses that as the secret value.

```yaml
secrets:
  GITHUB_TOKEN: "file:///home/user/.secrets/github_token"
```

Resolution: the file MUST exist and be readable. If absent or unreadable, the system logs a warning and does NOT inject.

## Detection Logic

The system detects which form a value is in by its prefix:

| Prefix | Form |
|---|---|
| `env://` | Form 2 (env reference) |
| `file://` | Form 3 (file reference) |
| (any other) | Form 1 (inline) |

This detection is **case-sensitive** and **prefix-only** (no URL parsing).

## Validation

The following checks are performed at `SkillsRegistry.__init__` (load time). All errors are reported together in a single `ConfigValidationError`.

1. **YAML parse**: if the file is not valid YAML, raise immediately with the parse error.
2. **Unknown skill**: if `skills.entries.<name>` contains a name that is not in the loaded skill set, raise with the unknown name(s) listed.
3. **Secret not in `requires.env`**: if `skills.entries.<name>.secrets.<ENV>` has a key that is NOT in the corresponding skill's `metadata.requires.env`, raise with the offending keys listed.

These checks are static (do not require any env vars or files to exist). Runtime resolution errors (Form 2 missing env, Form 3 missing file) are warnings, not errors.

## Example: Full Config File

```yaml
# ~/.agent_data/config.yaml
# Permission: chmod 600
# This file should be git-ignored and not shared.

skills:
  entries:
    # Skill 1: GitHub PR review (requires GITHUB_TOKEN only)
    github-pr-review:
      enabled: true
      secrets:
        GITHUB_TOKEN: "ghp_xxxxxxxxxxxxxxxxxxxx"

    # Skill 2: Multi-service bot (requires GITHUB_TOKEN + SLACK_BOT_TOKEN)
    # Demonstrates multi-secret support and a mix of inline + env-ref.
    multi-service-bot:
      secrets:
        GITHUB_TOKEN: "env://RAW_GH_TOKEN"      # value comes from shell env at run time
        SLACK_BOT_TOKEN: "xoxb-7890123456"      # inline plaintext

    # Skill 3: OpenAI summary (requires OPENAI_API_KEY)
    # Demonstrates file-ref for users with vault-mounted files.
    openai-summary:
      secrets:
        OPENAI_API_KEY: "file:///home/user/.secrets/openai_key"
```

## Backward Compatibility

- If the config file does not exist, the system operates in legacy mode (no secret injection). No error.
- If the config file is empty, same as not existing.
- If `skills.entries` is absent or `None`, same as legacy mode.
- A skill with no `secrets` block continues to work; it just doesn't have any secrets injected.

## Versioning

This contract is **v1**. Future versions may add:
- New `SecretRef` kinds (e.g., `vault://`, `aws-sm://`)
- Top-level fields beyond `skills` (e.g., `logging`, `limits`)
- Per-skill enable/disable semantics

Additions will be backward compatible (existing configs continue to work; new fields have sensible defaults).
