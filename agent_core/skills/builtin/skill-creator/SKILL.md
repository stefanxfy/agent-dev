---
name: skill-creator
description: "Use when creating, editing, or validating a SKILL.md for the agent_core skill system; covers frontmatter required fields, description-writing tips, and relative-path resolution."
homepage: https://github.com/openclaw/openclaw
metadata:
  os: ["darwin", "linux"]
---

# Skill Creator

When the user asks to create or edit a skill for this agent:

1. **Confirm purpose and trigger keywords** — what task should this skill handle, and which user-intent phrases should match its `description`?

2. **Create the directory** under the workspace skills root (default `skills/<name>/`), or under `agent_core/skills/builtin/<name>/` for an in-tree bundled skill.

3. **Write `SKILL.md`** with required frontmatter:
   - `name` (string; if omitted, falls back to the directory name)
   - `description` (string, **required** — this is the model's only trigger signal; keep it specific and keyword-rich; one line preferred)

   Optional frontmatter:
   - `homepage` (URL)
   - `disable-model-invocation: true` (hide from auto-selection; still invocable via `/name`)
   - `user-invocable: false` (do not expose as a slash command)
   - `metadata`: `{ always: bool, os: [str], requires: { bins: [...], anyBins: [...], env: [...], config: [...] } }`

4. **Body** = the specialized instructions the model reads after triggering. Reference sibling assets via relative paths (e.g. `references/cheatsheet.md`) — the system resolves them against this skill's own directory.

5. **Validate** by running the inspect command (`python3 scripts/skills_check.py`) — your skill must show `eligible` and `model_visible`.

## Common pitfalls

- A vague `description` ("helps with code") will never trigger; be specific ("Use when reviewing Python for thread-safety and concurrency bugs").
- Missing `description` → the skill is rejected at load time (other skills still load).
- Malformed YAML → the skill is skipped gracefully (agent never crashes).
- Single file size limit: 256 KB.
