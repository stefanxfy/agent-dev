# Implementation Plan: Skill Secret Injection

**Branch**: `feature/skill-secret-injection` | **Date**: 2026-07-06 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/002-skill-secret-injection/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command. See `.specify/templates/plan-template.md` for the execution workflow.

## Summary

Extend the agent_core skill system ([001-skill-system](specs/001-skill-system/spec.md)) with **runtime secret injection**. A skill author declares required env vars via `requires.env` in `SKILL.md`; the end user configures secret values (inline, env-reference, or file-reference) in a private config file; at agent-run time, the system injects the secrets into `process.env`, snapshots the pre-injection state, runs the agent, and restores `process.env` to its original state via a `try/finally` reverter. Multiple secrets per skill are supported; secret values are redacted from prompts, logs, and session persistence.

Technical approach:
- Add `SecretRef` and `SkillEntryConfig` dataclasses to `agent_core/skills/config.py`; extend `SkillsConfig` with an `entries: dict[str, SkillEntryConfig]` field
- Add new `agent_core/skills/env_overrides.py` module exporting `apply_skill_env_overrides(entries, config) -> Callable[[], None]` (the reverter)
- Integrate into `turn_chain.py:SkillsPromptHandler.__call__` with `try/finally`
- Validate config at `SkillsRegistry.__init__` (catch unknown skill names + secret names not in `requires.env` at startup, not at run time)
- Add `scripts/verify_skill_secrets_audit.py` for SC-005 (audit prompt/log/session for secret leakage)

## Technical Context

**Language/Version**: Python 3.11 (per `.specify/memory/constitution.md` §技术约束)
**Primary Dependencies**: `pydantic>=2.0.0`, `pyyaml>=6.0` (already in `requirements.txt` for memory_store), `pathlib`, `os`, `dataclasses` — all stdlib or already-installed
**Storage**: YAML config file at `~/.agent_data/config.yaml` (chmod 600, user-private); not used as a database, just a static mapping of skill name → secrets
**Testing**: `pytest` (existing test infrastructure; new tests follow the `tests/test_skill_*.py` pattern)
**Target Platform**: Linux + macOS (Python 3.11 cross-platform). Windows not explicitly supported; user assumes OS-level permission model.
**Project Type**: Library module within existing `agent_core/skills/` package; no new top-level project
**Performance Goals**: `apply_skill_env_overrides` MUST add < 5ms to run start (NFR-001); config validation runs once at registry construction (NFR-002)
**Constraints**:
- Backward compatible: existing skill configs without `entries` field MUST work unchanged
- No new third-party dependencies (all needed libs already in `requirements.txt`)
- Subprocess inheritance MUST work (Python `subprocess` default `env=os.environ` behavior)
**Scale/Scope**: Up to 50 secrets per skill, 100 total configured secrets (NFR-003). Single-user, single-machine deployment.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

### I. 自研优先 (Self-Built First) — **PASS**

This feature uses no new third-party libraries. `pyyaml` is already in `requirements.txt` (used by `memory_store.py`); `os.environ`, `pathlib`, `dataclasses` are stdlib. The reverter pattern is hand-rolled. No Agent framework is introduced. The feature is fully self-built on top of the existing `agent_core/skills/` package.

### II. 数据驱动，绝不编造 (Data-Driven, No Hallucination) — **PASS**

- All performance claims (NFR-001: < 5ms) will be measured via `time.perf_counter()` in tests, not assumed
- Config validation runs at startup, catching real misconfigurations (FR-005/FR-006) before any LLM call
- Secret values are not synthesized for tests — tests use fixture strings (e.g., `"test_token_abc123"`) and assert byte-equal absence in rendered output

### III. 文档即硬约束 (Documentation is a Hard Constraint) — **PASS**

This feature explicitly updates:
- `docs/agent_core-skill-system-design.md` (§6 deviation log + §3 data-flow diagram)
- `docs/skill/agent_core-skill-architecture.md` (§4.9 env_overrides section + §5.7 secret hygiene principle)
- `specs/001-skill-system/data-model.md` (entity additions cross-referenced)

The spec/plan/data-model/quickstart set is the hard constraint. No silent scope shrink — every deviation will be documented in `data-model.md` "Deviations" section.

### IV. 测试纪律 (Test Discipline — NON-NEGOTIABLE) — **PASS**

- 20 new test cases (per `checklists/requirements.md` SC-007)
- All fallbacks tested (e.g., `env://` missing → warn, not fail)
- `try/finally` reverter tested with simulated exception in downstream handler
- `scripts/verify_skill_secrets_audit.py` end-to-end audit script for SC-005
- Stub policy: no `return HandlerResult()` or `...` without `# intentionally stubbed: <reason>` docstring

### V. 不偷偷缩范围 (No Silent Scope Shrink) — **PASS**

- Spec covers all 3 user stories (P1, P2, P3) in full
- Vault integration (OOS-001), per-agent scoping (OOS-002), secret rotation (OOS-003), encrypted at-rest (OOS-004) are **explicitly** marked out of scope in the spec, not silently dropped
- 10 Assumptions documented (A-001 through A-010)
- Plan workflow follows the 10-step structured implementation I already laid out; each step has effort estimate + completion definition

### VI. 防御式务实工程 (Defensive & Pragmatic Engineering) — **PASS**

- Missing secrets → warn, not fail-fast (degrade gracefully; tool reports its own error)
- Reverter registered in `try/finally` — works even if LLM call or downstream handler throws
- Multi-skill env sharing: snapshot taken on first injection, restored to original (defends against order-dependent bugs)
- Defense in depth: config validation (static, at load) + runtime gating (dynamic, at run) both check "secret name ⊆ requires.env"
- `chmod 600` warning at config load time (FR-017, not hard-enforced — OS-dependent)

## Project Structure

### Documentation (this feature)

```text
specs/002-skill-secret-injection/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

The feature extends the existing `agent_core/skills/` package. No new top-level project, no `src/` or `backend/frontend` split.

```text
agent_core/skills/
├── __init__.py                    # MODIFIED: export SecretRef, SkillEntryConfig, apply_skill_env_overrides
├── config.py                      # MODIFIED: add SecretRef, SkillEntryConfig, SkillsConfig.entries
├── env_overrides.py               # NEW: resolve_secret(), apply_skill_env_overrides(), reverter
├── registry.py                    # MODIFIED: __init__ triggers validate_entries_against_skills()
└── (other files unchanged: types.py, frontmatter.py, prompt.py, snapshot.py,
     eligibility.py, commands.py, status.py, skill_store.py, skill_index.py, path_validator.py)

agent_core/
├── turn_chain.py                  # MODIFIED: SkillsPromptHandler wraps apply in try/finally
└── builder.py                     # MODIFIED: pass Config.skills entries to SkillsRegistry

tests/
├── test_skill_env_overrides.py    # NEW: 12 test cases (resolve + apply + reverter)
├── test_skill_secret_refs.py      # NEW: 5 test cases (inline/env/file/error)
├── test_skill_config_entries.py   # NEW: 3 test cases (validation)
└── test_skills_prompt_handler.py  # EXTENDED: +2 test cases (handler injects + reverts)

scripts/
└── verify_skill_secrets_audit.py  # NEW: SC-005 audit (prompt/log/session byte-equal absence)

docs/
├── agent_core-skill-system-design.md      # MODIFIED: §3 + §6.7
└── skill/agent_core-skill-architecture.md # MODIFIED: §4.9 + §5.7
```

**Structure Decision**: Extend the existing `agent_core/skills/` package (already established by `001-skill-system`). No new top-level directory. The new `env_overrides.py` is placed at the same layer as `eligibility.py` / `commands.py` (UseCase layer per `001-skill-system` architecture §2).

## Complexity Tracking

> **No violations to justify.** All 6 Constitution principles pass without contradiction. The feature is small (~750 lines total: ~250 LOC + ~300 test LOC + ~200 doc LOC), well within the >500-line threshold for plan-mode. The 10-step implementation plan I drafted in the prior conversation has each step ≤ 1.5 hours; no step is a hidden scope expansion.

If during Phase 1 a complexity emerges (e.g., we discover a circular import or a need to refactor the `turn_chain` handler signature), it will be added here with a justification.
