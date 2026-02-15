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

### Handoff pattern

Agent A finishes a ticket, writes a memento with a summary. Agent B starts the next ticket and sees Agent A's memento as "Prior Session" context. Knowledge transfers automatically through the governance layer.

This works especially well for sequential tasks:

```bash
# Agent A builds the feature
exo plan "add CSV export to reports"
# ... agent works, finishes, memento written ...

# Agent B writes tests for it
exo plan "write tests for CSV export"
# ... agent sees prior memento, knows what was built ...
```

No copy-pasting context between chat windows. The mementos do the work.

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
  install_command: "pip install -e ."
```

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

## Quick reference

| What you want | Command |
|---|---|
| Plan work | `exo plan "description"` |
| Start working | `exo next --owner name` |
| Check health | `exo doctor` |
| Check drift | `exo drift` |
| Review PR governance | `exo pr-check --base main` |
| Audit a session | `exo session-audit --ticket-id TID ...` |
| Record a learning | `exo reflect --pattern "..." --insight "..."` |
| Find existing tools | `exo tool-search "keywords"` |
| Clean up | `exo gc`, `exo gc-locks`, `exo session-cleanup` |
| Regenerate adapters | `exo adapter-generate` |
| Validate config | `exo config-validate` |
| Upgrade schema | `exo upgrade` |

---

## What's next?

- [FAQ](faq.md) -- quick answers to common questions
- [CLI Reference](../cli-reference.md) -- all commands
- [Agent docs](../agents/quickstart.md) -- deep-dive for agent developers
