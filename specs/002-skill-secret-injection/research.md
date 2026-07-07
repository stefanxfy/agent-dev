# Research: Skill Secret Injection

**Phase**: 0 (Outline & Research)
**Date**: 2026-07-06
**Status**: Resolved

## Purpose

Resolve any NEEDS CLARIFICATION items from the Technical Context and document design decisions that influenced the implementation plan. This file is the source of truth for **why** we chose a particular approach, not **how** to implement it (that's in `plan.md` and `tasks.md`).

## Resolved Unknowns

### 1. Config file location: where does `~/.agent_data/config.yaml` come from?

**Decision**: Use `~/.agent_data/config.yaml` as the default path, with environment variable override `AGENT_CONFIG_PATH`.

**Rationale**:
- The existing `001-skill-system` already uses `~/.agent_data/skills/` as the default `workspace_dir` (see `agent_core/skills/config.py:_default_workspace_dir`). The `~/.agent_data/` base is the project's established convention for per-user runtime config.
- Putting both the config file and the workspace under the same base directory simplifies permissions management: `chmod 700 ~/.agent_data/` covers everything.
- Environment variable override allows Docker / CI / multi-user scenarios.

**Alternatives considered**:
- `~/.<appname>/config.yaml` (XDG style): rejected — project doesn't follow XDG convention elsewhere
- `~/.config/agent_dev/config.yaml` (XDG_CONFIG_HOME): rejected — same reason
- Hardcoded path with no override: rejected — needs to be testable

**Reference**: `agent_core/skills/config.py:38-58` for the existing `_default_workspace_dir()` pattern we mirror.

### 2. Config validation timing: at load or at run?

**Decision**: Validate at `SkillsRegistry.__init__` (load time, once per process). Re-validate is unnecessary; the config file is re-read only on hot-reload.

**Rationale**:
- "Fail fast" is a Constitution principle (VI. 防御式务实工程). Surface config errors at process start, not at first LLM call.
- Validation cost is one-time ~10ms (rebuild snapshot to get skill names + requires.env sets). Acceptable per NFR-002.
- Hot-reload: when user changes config, the next `SkillsRegistry` construction re-validates. Existing registries do not need to be invalidated because the secret values are read fresh on each `apply_skill_env_overrides` call (FR-016).

**Alternatives considered**:
- Validate at every `apply_skill_env_overrides` call: rejected — would re-validate hundreds of times per session, wasteful
- Validate lazily on first use: rejected — defers the error to a confusing place
- No validation (trust the config): rejected — too easy to misconfigure; would surface as silent "secret never injected" bugs

### 3. `apply_skill_env_overrides` location: where does the reverter live?

**Decision**: New module `agent_core/skills/env_overrides.py`, exporting:
- `class SecretRef` (dataclass)
- `class SecretRefKind` (enum)
- `def resolve_secret(ref: SecretRef) -> str`
- `def apply_skill_env_overrides(entries: Sequence[SkillEntry], config: SkillsConfig) -> Callable[[], None]`
- `class SecretResolutionError(Exception)`

**Rationale**:
- Per `001-skill-system` architecture §2 (同心圆 layered architecture):
  - `SecretRef` and `SkillEntryConfig` are **data contracts** → Entity layer → `config.py` (alongside other Skill config)
  - `resolve_secret` is a **pure function** that reads from external sources → UseCase layer → new `env_overrides.py`
  - `apply_skill_env_overrides` has side effects on `os.environ` → **Adapter layer** (interfaces with the OS) → same `env_overrides.py` for cohesion (one place for all env-related logic)
- All "env overrides" related code in one module avoids splitting the abstraction across files.

**Alternatives considered**:
- Put everything in `config.py`: rejected — `config.py` should be pure data classes; IO is UseCase/Adapter
- Split into two modules (`secrets.py` + `env_overrides.py`): rejected — over-engineering for ~150 LOC

### 4. Subprocess inheritance: how do Bash tool subprocesses get the secrets?

**Decision**: Rely on Python's `subprocess` default behavior (inherits `os.environ` when `env=` is not explicitly passed). Verify in test 18.

**Rationale**:
- Python's `subprocess.run(cmd)` (no `env=`) → child process inherits parent's `os.environ`. Standard, well-documented, no code changes needed in `Bash` tool.
- This is the **most common pattern** in Python CLI tools; we follow the principle of least surprise.
- Adding explicit `env=os.environ` would be belt-and-suspenders; defer until proven necessary.

**Alternatives considered**:
- Explicit `env=os.environ` in Bash tool: deferred — adds noise; current default works
- Pass secrets via tool-call argument: rejected — leaks secrets into session.jsonl

**Verification**: Test 18 (`test_subprocess_inherits_env`) spawns a subprocess that prints `os.environ["X"]` and asserts the value is present.

### 5. Reverter function signature: `Callable[[], None]` vs context manager?

**Decision**: Plain function returning `Callable[[], None]` (the reverter). Caller uses `try/finally` to invoke.

**Rationale**:
- The reverter pattern is borrowed from openclaw's `applySkillEnvOverrides` (openclaw doc §4.6), which uses a synchronous function. Consistency with reference implementation.
- Context manager (`@contextmanager`) would be slightly more Pythonic but adds a layer of indirection. The `try/finally` pattern is already used extensively in the existing codebase (see `agent_core/turn_chain.py` for examples).
- Plain function is easier to test: assert "after reverter, env is back to pre-state" without `with` blocks.

**Alternatives considered**:
- `@contextmanager` (`with apply_skill_env_overrides(...) as reverter:`): deferred — not enough benefit
- Class-based (`with EnvOverride(...).open():`): rejected — heavier syntax for the same semantics

### 6. Secret value redaction in logs: how to enforce?

**Decision**: Logger emits **key names only** (e.g., `🧩 env injected: skill=github-pr key=GITHUB_TOKEN`). Values NEVER logged. Add an explicit warning if any future change accidentally logs a value.

**Rationale**:
- Manual discipline: the `env_overrides.py` module has explicit comments on every `logger.debug` / `logger.warning` call warning future maintainers not to add values.
- Test 14 (`test_no_secret_value_in_log_output`) spawns a fresh logger handler, runs `apply_skill_env_overrides`, asserts the captured log does not contain the secret string.
- (v1.1 enhancement: add a `logging.Filter` that redacts any string matching a registered secret key's value across the whole logger. Out of scope for v1.)

**Alternatives considered**:
- Global log filter for redaction: deferred to v1.1 (adds a second mechanism that needs to be kept in sync with the injection logic; risk of bugs)
- Disable logging entirely: rejected — loses diagnostic value for non-secret issues

### 7. Multi-skill env sharing: how is reverter order handled?

**Decision**: Snapshot is taken on **first** injection of an env name; subsequent injections of the same name **overwrite** the live value but do NOT re-snapshot. Reverter restores to the **original** pre-run value (not the value from the previous skill).

**Rationale**:
- Spec FR-011: "snapshot is taken at first injection; reverter restores to original pre-run value, not the value from the previous skill."
- The semantics match the openclaw `applySkillEnvOverrides` (openclaw doc §4.6 step 5: "env injected ... with reverter") and are the only sensible behavior — restoring to a non-original value would leak the previous skill's secret into the global env after the run.
- Implementation: `injected: dict[str, str | None]` where `None` means "was not set before". On first encounter of a key, snapshot the current value. On reverter, set to snapshot value (or pop if `None`).

**Alternatives considered**:
- Stack of values per key (LIFO revert): over-engineered — same result as snapshot-on-first in our use case (no run-time concurrent injection of same key from different skills)
- Per-skill reverters: rejected — would require bookkeeping for which skill injected which key, more code, same behavior

### 8. Config hot-reload: when does it kick in?

**Decision**: The `SkillsRegistry` is constructed once per process. Config values are read fresh on every `apply_skill_env_overrides` call (no caching of secret values). To pick up config changes:
- Easiest: restart the agent process
- Alternative: rebuild the registry (re-construct `SkillsRegistry(config)`) — already supported by the existing `SkillsRegistry` design (sig-based cache invalidation)

**Rationale**:
- Spec FR-016: "System MUST support secret hot-reload: changing the config file's secret values takes effect on the next run without requiring agent restart."
- Implementation: `apply_skill_env_overrides` reads `config.entries[name].secrets[k]` on each call, passes the `SecretRef` to `resolve_secret`, gets the latest value. No state to invalidate.
- Test 20 (`test_secret_hot_reload`) constructs a registry, runs with one secret value, mutates the config, runs again, asserts the new value is used.

**Alternatives considered**:
- File mtime watcher: rejected — over-engineering; the typical "next run" is fast enough
- Periodic re-read timer: rejected — same reason

### 9. Config file permissions check (FR-017): warn or fail?

**Decision**: Warn at config load time if the file is readable by group or other. Do NOT fail.

**Rationale**:
- "Warn, don't fail" matches Constitution VI (防御式务实工程) — degrade gracefully.
- On systems where `chmod` is restricted (Windows, certain CI containers), the user has no way to fix the permissions; failing would block them entirely.
- The warning is logged once at startup, doesn't spam.
- (Hard enforcement: out of scope for v1; users with strict security postures can wrap the registry construction in their own checks.)

**Alternatives considered**:
- Fail: rejected — too aggressive; breaks on platforms without Unix permissions
- Silent: rejected — defeats the purpose of FR-017

### 10. Test 18 (subprocess inheritance): which tool to test against?

**Decision**: Use the actual `Bash` tool from `agent_core/tools/builtin.py` if it's accessible from the test, else use a minimal `subprocess.run` mock. Verify both: (a) the env is in the parent process during the handler call, (b) `subprocess.run(...)` sees the same env.

**Rationale**:
- Direct test of `Bash` tool is more accurate but adds coupling to the `Bash` tool's signature (may change).
- Mocked `subprocess.run` test is sufficient to verify the **injection** (we control the value in `os.environ`; the assertion is that `subprocess.run` reads the same value).
- Test 18 will use the mocked approach for stability; if a regression occurs in `Bash` tool's subprocess invocation, the user will catch it manually (UI test).

**Alternatives considered**:
- Direct `Bash` tool test: deferred — coupling risk
- Skip this test entirely: rejected — SC-005 audit needs this guarantee

## Out-of-Scope Design Decisions (Documented for Future Reference)

These are decisions that were considered but explicitly deferred. They are listed here for future contributors to understand the boundary.

| Decision | Why Deferred | When to Revisit |
|---|---|---|
| Vault / external secret store integration | Spec OOS-001. Requires choosing a vault SDK + auth flow + key rotation. Significant scope. | v1.1 — track in `specs/003-skill-secret-vault/` if needed |
| Per-agent secret scoping | Spec OOS-002. Requires changes to `SkillsConfig` (per-agent config) and `SkillsRegistry` (per-agent snapshot). Larger refactor. | When multi-agent is a real use case |
| Encrypted at-rest config | Spec OOS-004. Requires OS keychain integration (e.g., `keyring` on macOS, DPAPI on Windows). Platform-specific. | When users complain about plaintext config in backups |
| `logging.Filter` for value redaction | Documented in decision 6. Adds a second mechanism that can drift from injection logic. | When audit log captures sensitive values in production |
| Async reverter support | Spec A-002 assumes single-threaded runs. Async reverter would need `asyncio` semantics. | When async agent loops land |
| Config file format other than YAML | Spec A-006 picks YAML for v1. JSON/TOML can be added without breaking changes (just register another parser). | When non-YAML configs are requested |

## Verification

All 10 resolved unknowns have a corresponding test case or code comment that proves the decision was honored. The Constitution Check is re-evaluated in `plan.md` post-design.
