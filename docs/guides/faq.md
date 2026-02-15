# FAQ

Quick answers to common questions.

## Setup

### "Can I use this on an existing project?"

Yes. `exo init` scans your repo (brownfield detection), detects languages, sensitive files, CI systems, and generates project-aware rules. Preview with `exo scan` first. It won't overwrite your existing CLAUDE.md -- it merges governance markers into it using `<!-- exo:governance:begin/end -->` sections. Your content stays untouched.

### "What does `exo init` actually create?"

- `.exo/` directory -- constitution, config, governance lock, tickets
- Agent configs -- CLAUDE.md, .cursorrules, AGENTS.md
- CI workflow -- `.github/workflows/exo-governance.yml`

All in your repo, all in git. Nothing hidden.

### "Do I need to use MCP?"

No. ExoProtocol works purely via CLI too. MCP just lets agents call governance commands natively (64 tools). Without MCP, agents read the bootstrap file and use the CLI. Either way, the governance is the same.

## During work

### "Why is my agent blocked?"

Common reasons:

1. **No active session** -- agent needs `exo session-start` first.
2. **Lock held by another actor** -- another agent has the ticket locked. Check with:
   ```bash
   exo session-scan
   ```
3. **Scope violation** -- agent tried to modify files outside ticket scope.
4. **Checks failed** -- governed checks didn't pass at session-finish.

When in doubt:

```bash
exo doctor
```

That gives you the full picture.

### "What's drift?"

Drift is a 0--1 score measuring how well work stayed within ticket boundaries. The formula: 40% scope violations + 30% file budget + 20% LOC budget + 10% boundary violations.

- Below 0.3 -- great, on track.
- 0.3 to 0.7 -- drifting, might want to split the ticket.
- Above 0.7 -- triggers a warning.

It's advisory -- it never blocks your agent.

### "How do I skip a check?"

Use break-glass:

```bash
exo session-finish --summary "..." --skip-check --break-glass-reason "CI is down, tests verified locally"
```

This gets logged in the audit trail. Don't make it a habit.

### "My agent crashed mid-session"

No problem:

```bash
exo session-scan          # see the stale session
exo session-cleanup       # auto-finish it with cleanup status
```

ExoProtocol tracks PIDs -- dead processes get flagged as stale automatically. The next `session-start` evicts them too.

### "What's a memento?"

A memento is the summary your agent writes at session-finish. It's stored in `.exo/memory/sessions/<ticket>/` and automatically injected into the next agent's bootstrap as "Prior Session" context. It's how agents pass knowledge to each other -- like shift notes.

### "How do I see what happened?"

- `exo status` -- ticket counts, active locks, dispatch candidates
- `exo session-scan` -- active and stale sessions
- `exo drift` -- overall governance health across all subsystems
- Browse `.exo/memory/sessions/` -- raw mementos per ticket

## Governance

### "How do I let agents modify kernel files?"

You don't -- kernel files (`exo/kernel/**`) are frozen by `RULE-KRN-001`. This is by design. Expanding the kernel requires an RFC and human approval through the evolution protocol. That's the whole point.

### "How do I change the rules?"

Edit `.exo/CONSTITUTION.md` (it's just markdown with YAML policy blocks), then:

```bash
exo build-governance      # recompile
exo adapter-generate      # update CLAUDE.md etc.
```

### "What if I want bigger budgets?"

Edit `.exo/config.yaml`:

```yaml
defaults:
  ticket_budgets:
    max_files_changed: 20
    max_loc_changed: 800
```

Or set per-ticket budgets when creating tickets with `--max-files` and `--max-loc`.

### "How do I add a new check?"

Add it to the allowlist in `.exo/config.yaml`:

```yaml
checks_allowlist:
  - pytest
  - npm test
  - your-custom-command
```

Then rebuild:

```bash
exo build-governance && exo adapter-generate
```

## Advanced

### "What's the difference between `exo audit` and `exo session-audit`?"

- `exo audit` -- governance integrity check. Does the lock match the constitution? Quick yes/no.
- `exo session-audit` -- adversarial review session where a *different* agent reviews another agent's work with a Red Team persona. It can also run PR governance checks with `--pr-base main --pr-head HEAD`.

### "Can multiple agents work on the same ticket?"

Technically yes, but it's not recommended. Tickets have locks -- only one actor can hold the lock at a time. If you want parallel work, split it into separate tickets. That's what the intent hierarchy is for.

### "How do I clean up old governance artifacts?"

```bash
exo gc --max-age-days 30     # mementos, cursors, bootstraps
exo gc-locks                 # expired remote leases
```

Both support `--dry-run` if you want to preview first.

## Still stuck?

- `exo doctor` -- comprehensive health check
- [Workflow Patterns](workflows.md) -- common usage patterns
- [CLI Reference](../cli-reference.md) -- all commands
- [Error Reference](../agents/error-reference.md) -- every error code explained
