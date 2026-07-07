# Quickstart: Skill Secret Injection

**Date**: 2026-07-06
**Audience**: Developers / users validating the Skill Secret Injection feature end-to-end
**Related**: [spec.md](spec.md), [data-model.md](data-model.md), [contracts/](contracts/)

## Overview

This quickstart validates the 7 Success Criteria from the spec (SC-001 through SC-007) end-to-end. It is **not** a complete implementation guide (see `plan.md` and `tasks.md` for that); it is a runnable validation sequence.

Each section maps to one or more Success Criteria. The sections should be runnable in order on a fresh checkout (after `pip install -r requirements.txt`).

## Prerequisites

```bash
# Ensure Python 3.11 and dependencies
python3.11 --version
pip install -r requirements.txt

# Ensure the project root is on PYTHONPATH
export PYTHONPATH=$(pwd):$PYTHONPATH
```

## Section 1: Single Secret End-to-End (SC-001, SC-002, SC-003)

**Goal**: Configure a skill with one secret, run the agent, verify the secret is injected during the run and the environment is restored after.

### Setup

1. Create a temporary workspace and skill:

```bash
mkdir -p /tmp/skill-secret-test/skills/echo-skill
cat > /tmp/skill-secret-test/skills/echo-skill/SKILL.md << 'EOF'
---
name: echo-skill
description: "A test skill that echoes $TEST_SECRET via the Bash tool. Used for validating env injection."
user-invocable: true
metadata:
  openclaw:
    requires:
      env: ["TEST_SECRET"]
---

# Echo Skill

When triggered, run:
```
echo "secret is: $TEST_SECRET"
```

Verify the value matches the configured secret.
EOF
```

2. Create a temporary config file:

```bash
mkdir -p /tmp/skill-secret-test/config
chmod 700 /tmp/skill-secret-test/config
cat > /tmp/skill-secret-test/config/config.yaml << 'EOF'
skills:
  entries:
    echo-skill:
      secrets:
        TEST_SECRET: "audit_token_abc123"
EOF
chmod 600 /tmp/skill-secret-test/config/config.yaml
```

3. Export the config path override:

```bash
export AGENT_CONFIG_PATH=/tmp/skill-secret-test/config/config.yaml
export SKILLS_PATHS__WORKSPACE_DIR=/tmp/skill-secret-test/skills
```

### Validation

```python
# scripts/verify_skill_secrets_e2e.py (excerpt — full script in scripts/)
import os
import sys
sys.path.insert(0, ".")

from agent_core.skills.config import SkillsConfig, from_env
from agent_core.skills.registry import SkillsRegistry
from agent_core.skills.env_overrides import apply_skill_env_overrides

# Load config
config = from_env()  # reads AGENT_CONFIG_PATH + SKILLS_PATHS__*
registry = SkillsRegistry(config)
snapshot = registry.snapshot()
assert any(s.name == "echo-skill" for s in snapshot.skills), "skill not loaded"

# Pre-state
assert "TEST_SECRET" not in os.environ, "TEST_SECRET should not be set before run"

# Inject
reverter = apply_skill_env_overrides(snapshot.entries, config)
try:
    assert os.environ["TEST_SECRET"] == "audit_token_abc123", \
        f"unexpected: {os.environ.get('TEST_SECRET')!r}"

    # Simulate tool call (subprocess)
    import subprocess
    out = subprocess.run(
        ["bash", "-c", "echo $TEST_SECRET"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "audit_token_abc123", \
        f"subprocess did not see secret: {out.stdout!r}"
finally:
    reverter()

# Post-state
assert "TEST_SECRET" not in os.environ, "TEST_SECRET not restored after reverter"
print("✓ Section 1 PASSED (SC-001, SC-002)")
```

**Expected output**: `✓ Section 1 PASSED (SC-001, SC-002)`

## Section 2: Multi-Secret (SC-003)

**Goal**: Configure a skill with 3 secrets, verify all 3 are injected and restored.

### Setup

```bash
mkdir -p /tmp/skill-secret-test/skills/multi-skill
cat > /tmp/skill-secret-test/skills/multi-skill/SKILL.md << 'EOF'
---
name: multi-skill
description: "Test skill requiring 3 env vars."
user-invocable: true
metadata:
  openclaw:
    requires:
      env: ["API_KEY", "DB_PASSWORD", "REDIS_URL"]
---
# Multi-skill (test fixture)
EOF

cat > /tmp/skill-secret-test/config/config.yaml << 'EOF'
skills:
  entries:
    multi-skill:
      secrets:
        API_KEY: "key_001_aaa"
        DB_PASSWORD: "db_pw_bbb"
        REDIS_URL: "redis://localhost:6379/0"
EOF
```

### Validation

```python
config = from_env()
registry = SkillsRegistry(config)
snapshot = registry.snapshot()
reverter = apply_skill_env_overrides(snapshot.entries, config)
try:
    assert os.environ["API_KEY"] == "key_001_aaa"
    assert os.environ["DB_PASSWORD"] == "db_pw_bbb"
    assert os.environ["REDIS_URL"] == "redis://localhost:6379/0"
finally:
    reverter()

assert "API_KEY" not in os.environ
assert "DB_PASSWORD" not in os.environ
assert "REDIS_URL" not in os.environ
print("✓ Section 2 PASSED (SC-003)")
```

## Section 3: Secret Sources (FR-004)

**Goal**: Verify all 3 secret source forms work.

### Setup

```bash
mkdir -p /tmp/skill-secret-test/skills/sources-skill /tmp/skill-secret-test/secrets
cat > /tmp/skill-secret-test/skills/sources-skill/SKILL.md << 'EOF'
---
name: sources-skill
description: "Test skill with 3 secrets in different source forms."
user-invocable: true
metadata:
  openclaw:
    requires:
      env: ["INLINE_VAL", "ENV_VAL", "FILE_VAL"]
---
EOF

echo "raw_env_value" > /tmp/skill-secret-test/secrets/env_raw
echo "file_value_with_trailing_whitespace  " > /tmp/skill-secret-test/secrets/file_value
chmod 600 /tmp/skill-secret-test/secrets/*

cat > /tmp/skill-secret-test/config/config.yaml << EOF
skills:
  entries:
    sources-skill:
      secrets:
        INLINE_VAL: "plain_text_inline"
        ENV_VAL: "env://ENV_RAW_VALUE"
        FILE_VAL: "file:///tmp/skill-secret-test/secrets/file_value"
EOF

export ENV_RAW_VALUE="raw_env_value"
```

### Validation

```python
# Inline, env-ref, and file-ref all resolve to their actual values
# (Full assertions in scripts/verify_skill_secrets_e2e.py §3)
print("✓ Section 3 PASSED (FR-004)")
```

## Section 4: Config Validation (SC-004)

**Goal**: Verify misconfigurations are caught at load time.

### Setup

```bash
# Case A: Unknown skill name
cat > /tmp/skill-secret-test/config/config.yaml << 'EOF'
skills:
  entries:
    does-not-exist:
      secrets:
        FAKE_KEY: "value"
EOF

# Case B: Secret name not in requires.env
cat > /tmp/skill-secret-test/skills/strict-skill/SKILL.md << 'EOF'
---
name: strict-skill
description: "Skill requiring only X."
user-invocable: true
metadata:
  openclaw:
    requires:
      env: ["X"]
---
EOF

cat > /tmp/skill-secret-test/config/config.yaml << 'EOF'
skills:
  entries:
    strict-skill:
      secrets:
        X: "x_value"
        EXTRA: "extra_value"   # not in requires.env — should fail
EOF
```

### Validation

```python
# Case A
try:
    SkillsConfig.from_yaml(open("/tmp/skill-secret-test/config/config.yaml").read())
    SkillsRegistry(config)
    assert False, "should have raised"
except ConfigValidationError as e:
    assert "does-not-exist" in str(e)
    print("✓ Case A: unknown skill name caught at load")

# Case B
try:
    config = ...
    SkillsRegistry(config)
    assert False, "should have raised"
except ConfigValidationError as e:
    assert "EXTRA" in str(e) and "strict-skill" in str(e)
    print("✓ Case B: secret not in requires.env caught at load")

print("✓ Section 4 PASSED (SC-004)")
```

## Section 5: Audit Script (SC-005)

**Goal**: Run `scripts/verify_skill_secrets_audit.py` to verify secret values do NOT appear in:
- `SkillSnapshot.prompt` (the `## Skills` system section)
- `agent.log` output
- `session.jsonl` persistence

```bash
python3 scripts/verify_skill_secrets_audit.py
```

**Expected output** (all checks pass):
```
[1/3] Checking SkillSnapshot.prompt does not contain secret values... ✓
[2/3] Checking agent.log does not contain secret values... ✓
[3/3] Checking session.jsonl does not contain secret values... ✓
✓ Audit PASSED (SC-005)
```

## Section 6: Hot-Reload (SC-006)

**Goal**: Change a config value and verify the next run uses the new value without process restart.

```python
# Initial run
config = from_env()  # TEST_SECRET = "first_value"
registry = SkillsRegistry(config)
reverter = apply_skill_env_overrides(registry.snapshot().entries, config)
assert os.environ["TEST_SECRET"] == "first_value"
reverter()

# Edit config file in place
import yaml
cfg_path = os.environ["AGENT_CONFIG_PATH"]
with open(cfg_path) as f:
    data = yaml.safe_load(f)
data["skills"]["entries"]["echo-skill"]["secrets"]["TEST_SECRET"] = "second_value"
with open(cfg_path, "w") as f:
    yaml.safe_dump(data, f)

# Next run (same process) sees new value
config2 = from_env()
registry2 = SkillsRegistry(config2)
reverter2 = apply_skill_env_overrides(registry2.snapshot().entries, config2)
assert os.environ["TEST_SECRET"] == "second_value", "hot-reload failed"
reverter2()

print("✓ Section 6 PASSED (SC-006)")
```

## Section 7: Full Test Suite (SC-007)

```bash
# Run all skill tests (existing 175+ + new 20)
python3 -m pytest tests/test_skill_*.py -q

# Run adjacent subsystems (no regression)
python3 -m pytest tests/test_builder.py tests/test_agent_core.py -q
```

**Expected output**:
```
tests/test_skill_env_overrides.py ............ [12 passed]
tests/test_skill_secret_refs.py .....          [5 passed]
tests/test_skill_config_entries.py ...         [3 passed]
tests/test_skills_prompt_handler.py .........  [existing 7 + new 2]
tests/test_skill_*.py (other 14 files)         [existing ~165 passed]
========================= 195+ passed in ~3s =========================
```

## Cleanup

```bash
rm -rf /tmp/skill-secret-test
unset AGENT_CONFIG_PATH SKILLS_PATHS__WORKSPACE_DIR ENV_RAW_VALUE
```

## Summary

After running all 7 sections, the feature is considered end-to-end validated. Any failure should be investigated; do not declare the feature "done" with any section failing.
