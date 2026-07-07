# Specification Quality Checklist: Security & Secret Hygiene

**Purpose**: Validate that secret hygiene, audit, and threat-model requirements in `spec.md` / `plan.md` are complete, clear, consistent, and testable.
**Created**: 2026-07-07
**Feature**: [spec.md](../spec.md) — Skill Secret Injection
**Audience**: Author self-check (lighter than PR-review / release-gate depth)
**Scope complement**: This file complements `requirements.md` (overall spec quality). It focuses narrowly on **security & secret-hygiene** requirements and does NOT re-test generic spec qualities already covered there.

---

## Threat Model & Scope

- [ ] CHK001 - Are the adversary classes (local user, malicious skill author, log-reader, backup-reader, etc.) and the surfaces they can reach (process.env / prompt / log / session.jsonl / subprocess output / core dump) explicitly enumerated in the spec? [Gap, Spec §FR-013..018]

## Secret Hygiene — Clarity

- [ ] CHK002 - Is "MUST NOT log secret values" (FR-014) precise about (a) which logger instances / sub-loggers are in scope, (b) which log levels (DEBUG/INFO/WARN/ERROR), and (c) whether log redaction covers full value or also partial substring matches (e.g., multi-line tokens, values containing escape sequences)? [Clarity, Spec §FR-014]

- [ ] CHK003 - Is the term "secret value" precisely scoped — does it cover only the resolved plaintext, or also the env-var name, the `SecretRef.kind` discriminator, the source path for `file://`, and the upstream env-var name for `env://`? Inconsistency here would let a redaction filter pass while still leaking via key names or paths. [Ambiguity, Spec §FR-014]

## Secret Hygiene — Coverage

- [ ] CHK004 - Beyond `session.jsonl` (FR-015), are all other persistence surfaces enumerated? Specifically: `agent.log`, any debug dumps, error reports, telemetry, crash dumps, in-memory `RunState` that gets serialized, and any temporary cache (e.g., a parsed-config cache). [Completeness, Spec §FR-015]

- [ ] CHK005 - Subprocess inheritance (FR-018) is verified for Bash. Are requirements defined for ALL other subprocess-spawning tools in the registry (Read on executables, Python interpreter invocations, custom user tools)? [Coverage, Spec §FR-018]

- [ ] CHK006 - Are requirements defined for the case where a child subprocess (e.g., `gh`, `curl`) **itself logs** the inherited env var — i.e., does the spec own hygiene all the way through, or does it delegate hygiene to the subprocess author? [Coverage, Spec §FR-018]

## Defense in Depth — Consistency

- [ ] CHK007 - Do the config-load validation rule (FR-005: secret keys ⊆ requires.env) and the runtime gating rule (FR-008: only inject declared names) describe **the same** constraint using the same wording? If they diverge subtly (e.g., one strict-subset, the other non-strict), there is a window where misconfig could pass one gate and fail the other. [Consistency, Spec §FR-005/FR-008]

- [ ] CHK008 - Are snapshot-on-first (FR-009, FR-011) and try/finally restore (FR-010) phrased so that **the same `EnvOverrideSnapshot` instance** is the only thing that knows the original values? If a reverter could read from a different store than the one that snapshotted, restore-different-value bugs become possible. [Consistency, Spec §FR-009/010/011]

## Fail-soft & Resolution Errors

- [ ] CHK009 - "MUST NOT fail the run" on missing secrets (FR-012) — is "fail" precisely defined? Does it cover: hard exception? logged error + exit? raised from within `apply_skill_env_overrides`? raised from within the downstream skill? Spec should explicitly say "log WARN + skip injection + continue run" so reviewers can grep for deviation. [Clarity, Spec §FR-012]

- [ ] CHK010 - Are requirements defined for the SECRET_REF kind (reserved for v1.1 vault integration)? At minimum: explicit NotImplementedError + log level, and a clear statement that this is **not** a silent fallback to INLINE. [Edge Case, Spec §OOS-001, data-model.md §SecretRefKind]

- [ ] CHK011 - Empty-string secret values: per Edge Cases, treated as valid (injected as empty string, reverted to previous). Are requirements precise about what happens when the **previous** value was also empty vs. unset? A reader could interpret both as "absent". [Edge Case, Spec §Edge Cases]

## Audit / Measurability

- [ ] CHK012 - SC-005 claims byte-equal absence "100% of the time across a representative set of test runs". Is the **representative set** enumerated (which skill configs, which prompt templates, which log handlers, which session schemas)? Without this, "100%" is unfalsifiable. [Measurability, Spec §SC-005]

- [ ] CHK013 - SC-002 (byte-equal env restore) is testable on the happy path. Is the exception path (run raises mid-flight) explicitly covered with a passing assertion? `try/finally` discipline degrades silently if exceptions are not in the test corpus. [Measurability, Spec §SC-002, §FR-010]

## Configuration & Permissions

- [ ] CHK014 - "MUST warn at load time if file is readable by group/other" (FR-017) — is the warning behavior precise about (a) which platforms emit the warning vs. silently skip (Windows / macOS / Linux), (b) which mode bits trigger it (group? world? ACLs?), (c) whether the warning is user-facing or log-only? [Clarity, Spec §FR-017, Assumption §A-003]

- [ ] CHK015 - Hot-reload (FR-016) — are requirements defined for the **race condition** where a run is in-flight when the config file changes mid-run? Does the run use the snapshot value (consistent) or re-read live (potentially mid-run switch)? Spec currently silent. [Coverage, Spec §FR-016, research.md §8]

## Assumptions & Out-of-Scope Tracking

- [ ] CHK016 - Assumption A-002 (single-threaded runs, reverter scope per-run) is documented as out of scope. Is there an **explicit regression test** that pins this assumption — e.g., a test that **fails** if anyone accidentally makes `EnvOverrideSnapshot` process-global instead of per-run? Without such a test, future contributors can silently break the assumption. [Assumption, Spec §A-002]

---

## Notes

- **Items intentionally out of scope here** (covered elsewhere or by domain checklist):
  - Overall spec completeness / requirement ID scheme / measurable SC-005 — see `requirements.md`
  - Contract format quality for `config-file-format.md` / `env-overrides-api.md` — see `contracts.md` (when generated)
  - Vault / encrypted-at-rest / per-agent scoping / rotation — all marked OOS-001..004 in spec §Out of Scope; not gaps
- **Risk-prioritized focus**: items CHK001/CHK004/CHK007/CHK012/CHK013/CHK015 are highest-impact (one missing requirement can create a secret-leak path); address those first if doing a partial pass.
- **Self-check depth**: this is intended as a 30-minute sanity sweep before opening the PR, not a release gate. Items can be marked `[x]` with a one-line justification in the PR description; do not skip without justification.