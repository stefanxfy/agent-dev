# Feature Specification: Skill Secret Injection

**Feature Branch**: `feature/skill-secret-injection`
**Created**: 2026-07-06
**Status**: Draft
**Input**: User description: "为 agent_core skill 系统增加多 secret env 注入能力 — SKILL.md 通过 requires.env 声明所需 secret,用户在配置中填入 secret 值(支持 inline / env:// / file:// 等来源),系统在 skill 触发时自动注入到 process.env,run 结束后 reverter 还原;支持一个 skill 多个 secret,防泄漏到 prompt / log / session 持久化。"

## Overview

This feature extends the agent_core skill system ([001-skill-system](specs/001-skill-system/spec.md)) with **runtime secret injection**. Today, a skill declaring `requires.env: [API_KEY]` only **checks** whether the env var is set; it does not **inject** it. Users must `export API_KEY=...` in their shell, polluting the global process environment. This spec adds a config-driven injection mechanism that:
- Stores secrets in a user-private config file (chmod 600), **never** in `SKILL.md`
- Injects secrets into `process.env` at the start of each run
- Restores `process.env` to its original state when the run ends (reverter pattern)
- Supports multiple secrets per skill and multiple skills sharing the same env name
- Validates config at load time to catch misconfigurations early

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Author declares a required secret; user configures it once (Priority: P1)

A skill author writes a `SKILL.md` declaring one or more required environment variables. The end user (skill consumer) adds the actual secret values to a private config file. On the next agent run, the secrets are automatically available to the skill's tool calls (e.g., `gh`, `curl`). The skill author never sees or stores the actual secret values; the user does not have to manually `export` anything in their shell.

**Why this priority**: This is the **core value proposition**. Without it, no skill that depends on API keys (GitHub, OpenAI, Slack, etc.) can work without shell pollution. This is the single biggest gap between the current system and a production-ready skill platform.

**Independent Test**: Configure a skill with `requires.env: [TEST_SECRET]`, set the secret in config, run the agent, observe that a tool call (`echo $TEST_SECRET`) in the agent's Bash invocation returns the configured value.

**Acceptance Scenarios**:

1. **Given** a skill with `requires.env: [TEST_SECRET]` and a user config containing `TEST_SECRET: "abc123"`, **When** the agent run starts, **Then** `process.env["TEST_SECRET"]` is `"abc123"` during the entire run, and tools (Bash) inherit this value when invoking child processes.
2. **Given** the same configuration, **When** the agent run ends, **Then** `process.env["TEST_SECRET"]` is restored to its pre-run value (or removed if it was not set before).
3. **Given** the same configuration, **When** the user inspects `session.jsonl` or `agent.log`, **Then** the string `"abc123"` does **not** appear anywhere.

---

### User Story 2 - Skill requires multiple secrets (Priority: P2)

A single skill needs multiple environment variables set (e.g., a `multi-service-bot` skill needs both `GITHUB_TOKEN` and `SLACK_BOT_TOKEN`). The user provides values for all of them in a single config block. At runtime, all secrets are injected and all are restored at the end of the run.

**Why this priority**: Real-world integrations often require multiple credentials. Without multi-secret support, complex skills must be split into multiple skills (each declaring one secret) or fall back to shell exports.

**Independent Test**: Configure a skill with `requires.env: [GITHUB_TOKEN, SLACK_BOT_TOKEN]`, provide both values in config, run the agent, observe that both `gh` and Slack API calls succeed (via mock subprocess assertions).

**Acceptance Scenarios**:

1. **Given** a skill with `requires.env: [A, B, C]` and config providing all three values, **When** the run starts, **Then** all three env vars are injected in the same `process.env`.
2. **Given** the same configuration, **When** the run ends, **Then** all three env vars are restored.
3. **Given** config providing values for `A, B, C` but the skill only requires `A, B` (extra `C` in config), **When** the run starts, **Then** only `A, B` are injected; `C` is silently ignored (or rejected at config validation, see FR-005).

---

### User Story 3 - Secrets sourced from different origins (Priority: P3)

A user can provide a secret value in one of several ways:
- **Inline** — write the value directly in the config file (simplest)
- **Env reference** — point to an environment variable already set elsewhere (e.g., `env://HOME_OPENAI_KEY`)
- **File reference** — point to a file containing the value (e.g., `~/.ssh/github_token` for users with strict secret management)

The system resolves the actual value at run time; the config file itself never contains plaintext for env/file references.

**Why this priority**: Different users have different security postures. Power users want their secrets in a vault or `pass` (file-based) without duplication; casual users want the convenience of inline strings. Supporting multiple sources accommodates both.

**Independent Test**: Configure a skill with `secrets: { GITHUB_TOKEN: "env://GITHUB_TOKEN_RAW" }`, set `GITHUB_TOKEN_RAW` in `process.env` before the run, verify the resolved value matches.

**Acceptance Scenarios**:

1. **Given** config `secrets: { X: "plain_value" }` (inline), **When** the run starts, **Then** `process.env["X"]` is `"plain_value"`.
2. **Given** config `secrets: { X: "env://RAW_X" }` and `os.environ["RAW_X"] = "real_value"` before the run, **When** the run starts, **Then** `process.env["X"]` is `"real_value"`.
3. **Given** config `secrets: { X: "file:///path/to/secret" }` and that file contains `"file_value"`, **When** the run starts, **Then** `process.env["X"]` is `"file_value"` (with trailing whitespace stripped).
4. **Given** config `secrets: { X: "env://MISSING_VAR" }` and `MISSING_VAR` is **not** set, **When** the run starts, **Then** `X` is **not** injected (logged as a warning), and the run continues without failing fast.

---

### Edge Cases

- **Skill not loaded**: User configures secrets for a skill name that does not exist in any source (bundled or workspace). The system **must** reject the config at load time with a clear error listing the unknown skill names.
- **Secret name not in requires.env**: User configures a secret whose name does not appear in the skill's `requires.env` list. The system **must** reject at load time.
- **Multiple skills share an env name**: Two skills both declare `requires.env: [API_KEY]`; config provides one value. The system injects once, reverter restores to the pre-run value.
- **Skill has no metadata (broken/load_error)**: The system skips env injection for that skill (the same skill that won't be in the prompt).
- **`process.env` had the key before run**: The system preserves the original value (overwrites during run, restores at end).
- **User provides empty string**: Treated as a valid (if unusual) value; the system injects the empty string and reverter restores it to the previous value.
- **Config file unreadable / malformed**: The system **must** fail at startup with a clear error indicating which file is broken and why.
- **Vault/external secret store**: Out of scope for v1; the `secret_ref` (e.g., `vault://...`) source type is reserved but raises a "not implemented" error if used. Documented in the spec but not implemented.
- **Concurrent turns / multi-agent**: A single agent run is single-threaded (per current architecture). Reverter scope is per-run. Concurrent turn safety is out of scope for v1.

## Requirements *(mandatory)*

### Functional Requirements

#### Secret Declaration (Author-side)

- **FR-001**: System MUST recognize `requires.env: [NAME1, NAME2, ...]` in `SKILL.md` frontmatter as the authoritative list of env var names the skill needs.
- **FR-002**: System MUST treat each env name in `requires.env` as a **declared dependency** that must be present at run time (or the run continues but the corresponding tool call will fail).

#### Secret Configuration (User-side)

- **FR-003**: System MUST support a user-private config file (default `~/.agent_data/config.yaml`, chmod 600) that maps each skill name to a `secrets` block.
- **FR-004**: The `secrets` block MUST map each env name to a value, where the value can be one of:
  - **inline string**: the literal value (e.g., `secrets: { GITHUB_TOKEN: "ghp_xxx" }`)
  - **env reference**: `env://VAR_NAME` to read from `process.env` at run time
  - **file reference**: `file:///absolute/path` to read the file contents at run time
- **FR-005**: System MUST validate at config load time that every `secrets` key is a subset of the corresponding skill's `requires.env` (rejection: hard error at startup).
- **FR-006**: System MUST validate at config load time that every config entry's skill name corresponds to a loaded skill (rejection: hard error at startup listing unknown names).

#### Secret Injection (Runtime)

- **FR-007**: System MUST inject configured secrets into `process.env` at the start of the agent run, **before** any LLM call, system prompt construction, or tool invocation.
- **FR-008**: System MUST **only** inject env names that appear in the skill's `requires.env` (defense in depth — config validation should already enforce this, but the runtime is a second gate).
- **FR-009**: System MUST snapshot the pre-injection value of each injected env name (or `None` if absent) so it can be restored.
- **FR-010**: System MUST restore `process.env` to its pre-run state at the end of the run, **regardless of whether the run succeeded or raised an exception** (try/finally guarantee).
- **FR-011**: System MUST handle multiple skills sharing the same env name: snapshot is taken at first injection; reverter restores to the **original** pre-run value, not the value from the previous skill.
- **FR-012**: System MUST NOT fail the run if a secret cannot be resolved (e.g., `env://MISSING`). The system MUST log a warning and continue; the corresponding tool call will fail with its own error if it actually needs the missing secret.

#### Secret Hygiene (Security)

- **FR-013**: System MUST NOT include secret values in the rendered `## Skills` system prompt section (byte-equal verification).
- **FR-014**: System MUST NOT log secret values in `agent.log` or any debug output. Key names may be logged; values MUST be redacted.
- **FR-015**: System MUST NOT persist secret values to `session.jsonl` or any session storage.
- **FR-016**: System MUST support **secret hot-reload**: changing the config file's secret values takes effect on the next run without requiring agent restart.
- **FR-017**: System MUST treat the config file as a sensitive resource: document the chmod 600 requirement; the system MAY warn at load time if the file is readable by group/other.

#### Subprocess Inheritance

- **FR-018**: System MUST ensure that secrets injected into `process.env` are inherited by child processes (Bash, Read of executable scripts, etc.) so external tools like `gh`, `curl`, `jq` can read them without further configuration.

### Non-Functional Requirements

- **NFR-001**: Secret resolution and injection MUST add < 5ms to run start latency (per `apply_skill_env_overrides` call).
- **NFR-002**: Config validation MUST run on `SkillsRegistry` construction (once per process start), not per run.
- **NFR-003**: System MUST support up to 50 secrets per skill and 100 total configured secrets without measurable performance impact.

### Key Entities *(include if feature involves data)*

- **SecretRef**: Represents how to resolve a single secret value. Attributes:
  - `kind`: one of `inline`, `env`, `file`, `secret_ref` (last one reserved for v1.1)
  - `value`: the literal string for `inline`, the env var name for `env`, the file path for `file`
- **SkillEntryConfig**: User configuration for a single skill. Attributes:
  - `enabled` (bool, default `true`): whether the skill is active
  - `secrets` (map of env name → `SecretRef`): the secret values to inject
- **SkillsConfig.entries**: A map of skill name → `SkillEntryConfig`, defaulting to `None` (no per-skill config). When `None`, the system operates in legacy mode (no injection).
- **EnvOverrideSnapshot** (internal): Holds the pre-injection value of each injected env name. Used by the reverter to restore state. Not a public entity.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A skill author can declare `requires.env: [X]` and the corresponding end user can configure a value in `~/.agent_data/config.yaml` without writing any Python code or modifying the shell environment.
- **SC-002**: After any agent run (success, exception, or interrupted), the system's `process.env` state is **byte-equal** to its pre-run state. Verified by snapshot + diff test.
- **SC-003**: A single skill with 5+ required env vars can be configured in one config block; all 5 are injected in a single `apply_skill_env_overrides` call and all 5 are restored at run end.
- **SC-004**: 100% of config misconfigurations (unknown skill name, secret name not in `requires.env`, malformed YAML) are caught at `SkillsRegistry` construction, with an error message that includes the offending path. Zero misconfigurations reach the LLM run.
- **SC-005**: An audit script (`scripts/verify_skill_secrets_audit.py`) verifies that the strings of all configured secrets do not appear in:
  - The rendered `SkillSnapshot.prompt` (the `## Skills` system section)
  - The `agent.log` output
  - The `session.jsonl` persistence
  100% of the time across a representative set of test runs.
- **SC-006**: Changing a secret value in the config file takes effect on the **next** agent run without process restart (hot-reload), verified by two consecutive runs with different config values.
- **SC-007**: All 20 new test cases (covering FR-001 through FR-018) pass; full skill regression suite (175+ existing tests) remains green.

## Assumptions

- **A-001**: The skill system from `001-skill-system` is already shipped and the `SkillMetadata.requires.env: tuple[str, ...]` field exists. This feature builds on top of it; no breaking changes to the existing skill format.
- **A-002**: A single agent run is single-threaded (no concurrent turns from the same agent). If multi-threaded turns are added in a future version, env reverter scope will need to be per-thread.
- **A-003**: The user has filesystem access and can set file permissions (`chmod 600`). On systems where this is impossible (e.g., Windows), the user accepts the platform's permission model.
- **A-004**: Vault / external secret store integration is **out of scope for v1**. The `secret_ref` source type is reserved but raises a "not implemented" error. Users requiring vault integration can use `env://` with their vault's CLI injecting the env var, or `file://` pointing to a vault-mounted file.
- **A-005**: The `secrets` block is the **only** way to provide secret values to skills in v1. There is no API for runtime secret injection from the agent loop itself (no `tool_call` argument overrides).
- **A-006**: Config file format is YAML for v1. JSON or TOML support can be added later without breaking changes.
- **A-007**: The default config file path is `~/.agent_data/config.yaml` (matching the existing `~/.agent_data/skills/` convention from `001-skill-system`). The path is overridable via environment variable.
- **A-008**: All skills are eligible for secret injection regardless of `user_invocable` or `disable_model_invocation` settings — a model-triggered skill and a slash-triggered skill both have the same access to configured secrets.
- **A-009**: The reverter pattern uses a function call to restore state, registered as a `try/finally` block. There is no async / await semantics in the reverter — synchronous restoration is sufficient.
- **A-010**: The Bash tool (and any other subprocess-spawning tool) inherits `process.env` by default. No explicit `env=` parameter passing is required; the system relies on Python's `subprocess` default behavior.

## Dependencies

- **D-001**: Requires the `001-skill-system` feature to be merged and available. Specifically, depends on:
  - `SkillMetadata.requires.env: tuple[str, ...]` (data-model §1)
  - `SkillsConfig` (data-model §9)
  - `SkillsRegistry` and `snapshot()` flow (architecture §2.3)
  - `SkillsPromptHandler` integration in `turn_chain.py` (architecture §4)
- **D-002**: Requires `Config` to have a path to the user-private config file. If not already present, the feature introduces a default `~/.agent_data/config.yaml` location.
- **D-003**: Does **not** require any new third-party dependencies. `pyyaml` (for config parsing) and `pathlib` (for file reading) are already in the project.

## Out of Scope

- **OOS-001**: Vault integration (HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager, etc.) — deferred to v1.1.
- **OOS-002**: Per-agent secret scoping (different agents see different secrets) — out of scope for v1; secrets are global per `Config`.
- **OOS-003**: Secret rotation policies (auto-rotation, expiry, revocation) — out of scope.
- **OOS-004**: Encrypted at-rest config files (using OS keychain, etc.) — out of scope; the user is expected to use `chmod 600` and OS-level disk encryption.
- **OOS-005**: Per-secret user prompts at run time (e.g., "enter your GitHub token") — out of scope; config-driven only.
- **OOS-006**: Secret audit logging (who accessed what when) — out of scope for v1.
