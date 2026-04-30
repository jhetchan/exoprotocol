# Config Reference

ExoProtocol configuration schema for `.exo/config.yaml`.

## File Location and Validation

- **Location**: `.exo/config.yaml`
- **Validation**: `exo config-validate` — checks structure, types, value ranges
- **Migration**: `exo upgrade` — backfills missing keys from defaults
- **Generation**: `exo init` — creates config from scan results (brownfield) or defaults (greenfield)

## Schema

### version

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `version` | int | 1 | Config schema version |

### defaults

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `defaults.ticket_budgets.max_files_changed` | int | 12 | Default max files per ticket |
| `defaults.ticket_budgets.max_loc_changed` | int | 400 | Default max lines of code per ticket |
| `defaults.ticket_checks` | list | [] | Default checks required for all tickets |

### checks_allowlist

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `checks_allowlist` | list[str] | ["npm test", "npm run lint", "pytest", "python -m pytest", "python3 -m pytest"] | Commands allowed in ticket checks |

### do_allowlist

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `do_allowlist` | list[str] | ["npm run build"] | Commands allowed in `exo do` without explicit approval |

### recall_paths

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `recall_paths` | list[str] | [".exo", "docs"] | Paths scanned for session bootstrap context injection |

### self_evolution

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `self_evolution.trusted_approvers` | list[str] | ["agent:trusted"] | Actors allowed to approve practice changes |
| `self_evolution.governance_cooldown_hours` | int | 24 | Hours between governance changes |

### scheduler

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `scheduler.enabled` | bool | false | Enable lane-aware dispatch scheduling |
| `scheduler.global_concurrency_limit` | int \| null | null | Max concurrent sessions across all lanes |
| `scheduler.lanes` | list | [] | Lane definitions (each: name, tags, concurrency_limit) |

### control_caps

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `control_caps.decide_override` | list[str] | ["cap:override"] | Actors allowed to override policy decisions |
| `control_caps.policy_set` | list[str] | ["cap:policy-set"] | Actors allowed to modify policy rules |
| `control_caps.cas_head` | list[str] | ["cap:cas-head"] | Actors allowed to perform CAS head operations |

### git_controls

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `git_controls.enabled` | bool | true | Enable git-based lock/diff enforcement |
| `git_controls.strict_diff_budgets` | bool | true | Enforce LOC budgets via git diff |
| `git_controls.enforce_lock_branch` | bool | true | Require lock branch for sessions |
| `git_controls.auto_create_lock_branch` | bool | true | Auto-create lock branch on session-start |
| `git_controls.auto_switch_lock_branch` | bool | true | Auto-switch to lock branch on session-start |
| `git_controls.require_clean_worktree_before_do` | bool | true | Require clean worktree before `exo do` |
| `git_controls.stale_lock_drift_hours` | int | 24 | Hours before lock branch considered stale |
| `git_controls.base_branch_fallback` | str | "main" | Fallback base branch for diff/merge operations |
| `git_controls.ignore_paths` | list[str] | [".exo/logs/**", ".exo/cache/**", ".exo/locks/**", ".exo/tickets/**", "**/__pycache__/**", "**/*.pyc"] | Paths excluded from git diff budget calculations |

### privacy

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `privacy.commit_logs` | bool | false | Commit `.exo/logs/**` to version control |
| `privacy.redact_local_paths` | bool | true | Redact absolute paths from session artifacts |

When `commit_logs=false`, `.exo/logs/` is auto-added to `.gitignore`.

### private_memory

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `private_memory.enabled` | bool | true | Enable private memory leak detection |
| `private_memory.watch_paths` | list[str] | [] | Paths to agent-specific memory files (opt-in) |

Paths expanded with `~` tilde. Detection runs at session-finish if any watched file's mtime is newer than session start.

### coherence

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `coherence.enabled` | bool | true | Enable semantic coherence checks |
| `coherence.co_update_rules` | list | [] | File pairs that must change together (each: files, label) |
| `coherence.docstring_languages` | list[str] | ["py"] | Languages for docstring freshness checks |
| `coherence.skip_patterns` | list[str] | [] | Globs to skip in coherence checks |

Co-update rule schema:
```yaml
co_update_rules:
  - files: ["src/module.py", "docs/module.md"]
    label: "Description of why these must change together"
```

### follow_up

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `follow_up.enabled` | bool | true | Enable chain reaction follow-up ticket creation |
| `follow_up.max_per_session` | int | 5 | Max follow-up tickets auto-created per session-finish |

Detection rules trigger follow-ups for: uncovered code, unbound features, high drift, uncovered requirements, unused tools.

### ci

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `ci.drift_threshold` | float | 0.7 | Drift score threshold for PR governance checks |
| `ci.python_version` | str | "3.11" | Python version for CI workflow |
| `ci.install_command` | str | "pip install exoprotocol==&lt;generating version&gt;" | Command to install ExoProtocol in CI. Default pins to the version that generated the workflow; override (e.g. `pip install -e .`) only if your repo IS exoprotocol or you vendor it locally. |
| `ci.app_install_command` | str | auto-detected (see below) | Optional second install step that runs after ExoProtocol install and before `Run governed checks`. Use it so commands in `checks_allowlist` (pytest, mypy, etc.) find the application's package and test/dev extras. **When the key is omitted**, the generator auto-detects from `pyproject.toml`: prefers `pip install -e ".[test]"` if `[project.optional-dependencies].test` is defined, falls back to `[dev]`, then plain `pip install -e .`; if no `pyproject.toml`/`[project]` table exists, the step is skipped. **Set the key explicitly** to override (e.g. `pip install -r requirements-dev.txt`, `poetry install --no-root --with test`); set it to `""` to opt out (non-Python repos, governance-only repos). |

Used by `exo adapter-generate --target ci` to generate `.github/workflows/exo-governance.yml`.

## Value Constraints

- `defaults.ticket_budgets.max_files_changed` > 0
- `defaults.ticket_budgets.max_loc_changed` > 0
- `git_controls.stale_lock_drift_hours` >= 0
- `self_evolution.governance_cooldown_hours` >= 0
- `ci.drift_threshold` between 0.0 and 1.0
- `ci.python_version` must be valid Python version string
- All list items in `checks_allowlist`, `do_allowlist`, `recall_paths` must be strings

## Manifest-First Principle

All values in this config are the **source of truth**. Code must:
1. Load values from config at runtime (never hardcode)
2. Write tests that vary config values and verify output follows
3. Fail config validation if values are out of range

See CLAUDE.md "Test-Driven, Manifest-First Workflow" for enforcement protocol.
