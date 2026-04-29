# Workflow Patterns

Common ways to use ExoProtocol in your day-to-day development.

---

## Solo vibe-coding

The simplest flow -- you and one AI agent.

1. `exo plan "describe what you want"` -- generates tickets
2. `exo next --owner your-name` -- dispatches the highest priority ticket and acquires a lock
3. Open your AI editor (Claude Code, Cursor, etc.) -- it sees the governance bootstrap
4. Let the agent work. It sees scope, budget, checks, and prior session context.
5. Agent finishes -- memento written, drift scored, lock released
6. `exo status` to see what's done and what's next
7. Repeat

That's the whole loop. The agent handles the session lifecycle (`session-start` / `session-finish`) automatically because the governance directives are in the adapter file it reads on startup (e.g., `CLAUDE.md`).

**Tips:**

- The agent reads `.exo/cache/sessions/<actor>.bootstrap.md` automatically. You don't need to paste anything.
- Mementos live in `.exo/memory/sessions/<ticket>/` -- skim them to see what the agent did, what decisions it made, and what drift it accumulated.
- If an agent seems stuck or confused, run `exo doctor` to check for governance issues.
- `exo drift` gives you a composite health-check across all governance subsystems at once.

---

## Multi-agent development

Two or more agents working on the same repo. This is where ExoProtocol earns its keep.

### Same ticket (pair programming)

Not recommended. Lock contention will block one agent.

If you really need two agents on the same ticket, suspend one session before the other starts:

```bash
# Agent A pauses
EXO_ACTOR=agent:claude exo session-suspend --ticket-id TICKET-001

# Agent B picks up
EXO_ACTOR=agent:cursor exo session-start --ticket-id TICKET-001 ...
```

### Separate tickets (parallel work)

This is the happy path. Each agent gets its own ticket with its own scope.

ExoProtocol handles coordination:

- **Scope overlap detection** -- at `session-start`, ExoProtocol checks if your ticket's scope overlaps with any active sibling session. You'll see a "Start Advisories" warning in the bootstrap if it does.
- **Sibling awareness** -- the bootstrap includes a "Sibling Sessions" section showing who else is working, on what ticket, on which branch.
- **Stale branch detection** -- if your branch is behind `main`, you'll get an advisory at session start.

Keep ticket scopes non-overlapping and you'll rarely hit conflicts.

### Handoff pattern (explicit)

For same-ticket handoffs, use `session-handoff` — it atomically finishes Agent A's session, writes a handoff record, releases the lock, and injects context into Agent B's bootstrap:

```bash
# Agent A builds the feature, then hands off
EXO_ACTOR=agent:claude-opus exo session-handoff \
  --to agent:claude-sonnet --ticket-id TICKET-001 \
  --summary "Built API endpoints, tests not written" \
  --reason "Needs testing expertise" \
  --next-step "Write integration tests for /api/users"

# Agent B picks up — handoff context auto-injected into bootstrap
EXO_ACTOR=agent:claude-sonnet exo session-start \
  --ticket-id TICKET-001 --vendor anthropic --model claude-sonnet-4-5
```

Agent B's bootstrap will include a "Handoff Context" section with Agent A's summary, reason, and next steps. The handoff record is consumed on start — no stale context.

### Handoff pattern (implicit)

For cross-ticket handoffs, mementos do the work automatically. Agent A finishes a ticket, writes a memento with a summary. Agent B starts the next ticket and sees Agent A's memento as "Prior Session" context.

```bash
# Agent A builds the feature
exo plan "add CSV export to reports"
# ... agent works, finishes, memento written ...

# Agent B writes tests for it
exo plan "write tests for CSV export"
# ... agent sees prior memento, knows what was built ...
```

No copy-pasting context between chat windows.

---

## Code review with audit sessions

When an agent finishes work and you want a second opinion:

```bash
EXO_ACTOR=agent:reviewer exo session-audit \
  --ticket-id TICKET-001 \
  --vendor anthropic --model claude-opus-4-6 \
  --pr-base main --pr-head feature-branch
```

What happens under the hood:

- The audit agent gets an adversarial "Red Team Auditor" persona -- it's told to look for problems, not rubber-stamp.
- **Context isolation** -- the auditor can't see `.exo/cache/` or `.exo/memory/`. This prevents it from just agreeing with the original agent's reasoning.
- **PR governance report** -- injected into the bootstrap with ungoverned commits, scope violations, and drift scores.
- **Model mismatch warning** -- if the auditor is the same model as the writer, ExoProtocol flags weak independence.

You can also customize the auditor persona by creating `.exo/audit_persona.md` with your own review directives.

---

## Enforcement hooks

ExoProtocol can mechanically enforce governance checks at git and agent runtime level.

### Git pre-commit hook

```bash
exo hook-install --git
```

This writes `.git/hooks/pre-commit` that runs `exo check` before every commit. If checks fail, the commit is blocked. Use `--no-verify` to bypass in emergencies.

### Claude Code PreToolUse enforcement

```bash
exo hook-install --enforce
```

This installs a Claude Code `PreToolUse` hook that intercepts `git commit` and `git push` commands and runs `exo check` first. If checks fail, the tool call is blocked — the agent cannot push unchecked code.

### Governed push

Instead of bare `git push`, use:

```bash
exo push                         # runs exo check, then git push
exo push --remote origin         # explicit remote
exo push --force                 # uses --force-with-lease
```

This is the recommended way to push code in a governed repo. All adapter files (CLAUDE.md, .cursorrules, AGENTS.md) include a directive telling agents to use `exo push`.

### Promoting learnings to checks

When an agent discovers a pattern worth enforcing mechanically:

```bash
exo reflect --pattern "Pushing without running ruff" \
            --insight "Always run ruff before pushing" \
            --promote-check "ruff check ."
```

The `--promote-check` flag adds the command to `checks_allowlist` in config, so it's enforced by `exo check`, `exo push`, session-finish, and pre-commit hooks.

---

## CI integration

Set up PR governance checks in one command:

```bash
exo adapter-generate --target ci
```

This creates `.github/workflows/exo-governance.yml` which:

- Runs `exo pr-check` on every PR
- Verifies every commit traces back to a governed session
- Flags ungoverned commits, scope violations, and high drift
- Uploads a governance report as a JSON artifact

Configure thresholds in `.exo/config.yaml`:

```yaml
ci:
  drift_threshold: 0.7
  python_version: "3.11"
  # Default pins exoprotocol to the version that generated the workflow.
  # Override only if your repo IS exoprotocol or you vendor it locally
  # (e.g. install_command: "pip install -e .").
  install_command: "pip install exoprotocol==<version>"
```

> Why a pin and not `pip install -e .`? In a governed application repo, `pip install -e .`
> installs the application package, not exoprotocol — the next workflow step then fails
> with `ModuleNotFoundError: No module named 'exo'`.

You can also run `exo pr-check` locally before pushing:

```bash
exo pr-check --base main --head HEAD
```

This gives you the same verdict the CI will produce, without waiting for a pipeline.

---

## Managing features and requirements

Both of these are optional. ExoProtocol works fine without them. But if you want traceability, here's how.

### Feature manifest

Track which code belongs to which feature:

1. Define features in `.exo/features.yaml` with status and file globs
2. Tag code with `# @feature:<id>` and `# @endfeature`
3. Run `exo trace` to check for uncovered code, invalid tags, or deprecated usage
4. Run `exo prune` to auto-remove code blocks for deleted features

```yaml
# .exo/features.yaml
features:
  - id: csv-export
    name: CSV Export
    status: active
    files:
      - "src/export/**"
```

```python
# @feature:csv-export
def export_to_csv(data):
    ...
# @endfeature
```

### Requirement manifest

Track what the system must do:

1. Define requirements in `.exo/requirements.yaml` with priority and tags
2. Annotate code with `# @req:<id>` or `# @implements:<id>`
3. Run `exo trace-reqs` to find orphan refs or uncovered requirements

```yaml
# .exo/requirements.yaml
requirements:
  - id: REQ-AUTH-001
    description: Users must authenticate before accessing protected routes
    priority: high
    status: active
```

---

## Operational learnings with reflections

When an agent discovers a gotcha or pattern, it should record it:

```bash
exo reflect --pattern "pytest fixtures don't work with async" \
            --insight "Use pytest-asyncio and mark fixtures with @pytest_asyncio.fixture"
```

Reflections are stored in `.exo/memory/reflections/` and automatically injected into future session bootstraps. Every agent benefits from what any agent learned.

View current reflections:

```bash
exo reflections
exo reflections --severity high
```

Dismiss stale ones:

```bash
exo reflect-dismiss REF-003
```

---

## Tool reuse across sessions

Agents build utility functions. Without tracking, they'll rebuild the same thing next session.

After building a reusable utility:

```bash
exo tool-register src/utils/csv_helpers.py parse_csv --description "Parse CSV with header detection"
```

Before writing new utilities:

```bash
exo tool-search "csv parse"
```

Check for underused or duplicated tools:

```bash
exo tool-suggest
```

The tool registry lives in `.exo/tools.yaml` and gets injected into agent bootstraps so they know what's already available.

---

## `.exo/` git tracking

Governance files must be committed to git. Without this, CI pipelines can't run `exo pr-check`, other agents can't see governance rules, and team members lose visibility into what's being enforced.

`exo install` handles this automatically — it commits `.exo/CONSTITUTION.md`, `.exo/config.yaml`, `.exo/governance.lock.json`, `.exo/tickets/`, and `.exo/.gitignore` as its final step. Ephemeral data (`cache/`, `logs/`, `locks/`, `memory/sessions/`) is excluded via `.exo/.gitignore`.

If you've already initialized but haven't committed:

```bash
git add .exo/ && git commit -m "chore: track governance files"
```

`exo doctor` checks tracking status — it will fail if `.exo/` is untracked in a git repo. Session-start also injects a bootstrap warning so agents know governance is local-only.

For teams wanting separate git histories for app code and governance state, use the sidecar worktree pattern:

```bash
exo sidecar-init --branch exo-governance --sidecar .exo
```

This gives you dual timelines: app code on `main`, governance state on `exo-governance`. Auto-commits at every lifecycle boundary (start, finish, suspend, resume) keep the governance branch up to date without polluting your app history.

---

## Cleaning up

### Stale sessions

```bash
exo session-scan        # see what's stuck
exo session-cleanup     # auto-finish stale sessions (48h threshold)
```

Sessions from dead processes are automatically flagged as stale via PID liveness checks.

### Old artifacts

```bash
exo gc --max-age-days 30    # clean mementos, cursors, bootstraps older than 30 days
exo gc --dry-run            # preview what would be removed
```

### Expired remote locks

```bash
exo gc-locks --list         # see what's out there
exo gc-locks                # clean expired lock refs from remote
```

### Closing tickets

Agents set ticket status to `review` or `done` at `session-finish`. You can also manually close tickets by editing the YAML files in `.exo/tickets/`.

---

## Evolving governance

Your constitution (`.exo/CONSTITUTION.md`) is yours to edit.

1. Edit the constitution -- add rules, change deny patterns, adjust budgets
2. `exo build-governance` -- recompile into the governance lock
3. `exo adapter-generate` -- regenerate `CLAUDE.md`, `.cursorrules`, etc.
4. `exo audit` -- verify everything is consistent

For agent-proposed changes, ExoProtocol has a formal evolution protocol:

```
observe -> propose -> approve -> apply -> distill
```

Agents can observe issues and propose changes, but applying governance changes requires explicit human approval. That's `RULE-EVO-001` -- practice is mutable, governance requires a human in the loop.

---

## Observability

### Governance metrics

Get aggregate stats for dashboards:

```bash
exo metrics
```

Returns verify pass rate, drift distribution, ticket throughput, actor breakdown, and mode counts (work vs audit).

### Fleet drift

See drift across all active agents:

```bash
exo fleet-drift
```

Shows per-agent drift scores, stale sessions, and fleet-level averages. Useful for multi-agent teams to spot which agents are drifting.

### Structured traces

Export session history as OTel-compatible JSONL:

```bash
exo export-traces                          # write to .exo/logs/traces.jsonl
exo export-traces --since 2026-02-01T00:00:00+00:00  # only recent sessions
exo export-traces --no-write               # return spans without writing
```

Each session becomes an OTel span with `exo.*` attributes. Import into Jaeger, Grafana Tempo, or any OTel backend.

---

## CI failure auto-fix

When CI fails, `exo ci-fix` fetches the failed run logs via the `gh` CLI, parses errors into structured entries, and can auto-fix + push:

```bash
# Inspect the latest failed CI run
exo ci-fix

# Auto-fix what's fixable (e.g., ruff format) and push
exo ci-fix --apply --push

# Inspect a specific run
exo ci-fix --run-id 12345678
```

Supported error parsers:
- **ruff format** — detects "N files would be reformatted", auto-fixable via `ruff format`
- **ruff lint** — parses `file:line:col: CODE message` entries
- **pytest** — extracts `FAILED path::Class::method` entries
- **Python compile** — catches `SyntaxError`, `IndentationError`, `TabError`

Requires `gh` CLI installed and authenticated (`gh auth login`).

---

## SDK integrations

### OpenAI Agents SDK

Wrap governed sessions around agent runs:

```python
from exo.integrations.openai_agents import ExoRunHooks
from agents import Runner

hooks = ExoRunHooks(repo=".", ticket_id="TKT-...", actor="agent:openai")
result = await Runner.run(agent, hooks=hooks)
```

Install the extra: `pip install exoprotocol[openai-agents]`

### Claude Code hooks

Installed via `exo hook-install`. Automatically starts/finishes governed sessions on Claude Code lifecycle events. No code changes needed.

---

## Quick reference

| What you want | Command |
|---|---|
| Plan work | `exo plan "description"` |
| Start working | `exo next --owner name` |
| Check health | `exo doctor` |
| Check drift | `exo drift` |
| Fleet drift | `exo fleet-drift` |
| Governance metrics | `exo metrics` |
| Export traces | `exo export-traces` |
| Review PR governance | `exo pr-check --base main` |
| Audit a session | `exo session-audit --ticket-id TID ...` |
| Hand off to another agent | `exo session-handoff --to agent:x --ticket-id TID --summary "..."` |
| Record a learning | `exo reflect --pattern "..." --insight "..."` |
| Promote learning to check | `exo reflect --pattern "..." --insight "..." --promote-check "cmd"` |
| Install git enforcement | `exo hook-install --git` |
| Install Claude Code enforcement | `exo hook-install --enforce` |
| Governed push | `exo push` |
| Find existing tools | `exo tool-search "keywords"` |
| Preview sandbox policy | `exo sandbox-policy` |
| Fix CI failures | `exo ci-fix [--apply] [--push]` |
| Clean up | `exo gc`, `exo gc-locks`, `exo session-cleanup` |
| Regenerate adapters | `exo adapter-generate` |
| Validate config | `exo config-validate` |
| Upgrade schema | `exo upgrade` |

---

## What's next?

- [FAQ](faq.md) -- quick answers to common questions
- [CLI Reference](../cli-reference.md) -- all commands
- [Agent docs](../agents/quickstart.md) -- deep-dive for agent developers
