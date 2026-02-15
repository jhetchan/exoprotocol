# Agent Quickstart

A step-by-step guide for AI agents operating in ExoProtocol-governed repositories.

## Prerequisites

- Python >= 3.10
- Install ExoProtocol:
  ```bash
  pip install exoprotocol
  ```

## Step 1: Initialize governance

Run initialization to scan the repository and generate governance artifacts:

```bash
exo init
```

This creates:
- `.exo/` — governance directory (constitution, config, features, requirements)
- `CLAUDE.md` — Claude-specific governance preamble
- `AGENTS.md` — Generic agent governance preamble
- `.cursorrules` — Cursor-specific governance preamble
- `.github/workflows/exo-governance.yml` — CI enforcement workflow

The scanner detects your language, build system, and sensitive files to generate project-aware rules.

## Step 2: Configure MCP

Add ExoProtocol as an MCP server to your agent configuration.

### Claude Code

```json
{
  "mcpServers": {
    "exoprotocol": {
      "command": "python3",
      "args": ["-m", "exo.mcp_server"],
      "env": {
        "EXO_ACTOR": "agent:claude"
      }
    }
  }
}
```

### Cursor

```json
{
  "mcpServers": {
    "exoprotocol": {
      "command": "python3",
      "args": ["-m", "exo.mcp_server"],
      "env": {
        "EXO_ACTOR": "agent:cursor"
      }
    }
  }
}
```

### Generic MCP client

```json
{
  "mcpServers": {
    "exoprotocol": {
      "command": "python3",
      "args": ["-m", "exo.mcp_server"],
      "env": {
        "EXO_ACTOR": "agent:yourname"
      }
    }
  }
}
```

## Step 3: Create work items

Generate a plan from a high-level goal:

```bash
exo plan "add user authentication"
```

This creates:
- An intent ticket (INTENT-001) with brain dump, boundary, success conditions
- Child task tickets with scope constraints

Or create tickets manually:

```bash
exo intent-create \
  --brain-dump "Users need login/logout/signup" \
  --boundary "No OAuth, local auth only" \
  --success-condition "Tests pass, no hardcoded secrets" \
  --risk high \
  --scope-allow "src/auth/**" \
  --max-files 8

exo ticket-create \
  --kind task \
  --parent INTENT-001 \
  --title "Implement password hashing" \
  --scope-allow "src/auth/hash.py" \
  --max-files 2
```

## Step 4: Start a governed session

Acquire the next available ticket:

```bash
exo next --owner agent:claude
```

Start a session for the locked ticket:

```bash
EXO_ACTOR=agent:claude python3 -m exo.cli session-start \
  --ticket-id TKT-001 \
  --vendor anthropic \
  --model claude-code \
  --task "Implement password hashing with bcrypt"
```

Or use the MCP tool:

```
exo_session_start(
  ticket_id="TKT-001",
  vendor="anthropic",
  model="claude-code",
  task="Implement password hashing with bcrypt"
)
```

This generates a bootstrap prompt at:
```
.exo/cache/sessions/agent-claude.bootstrap.md
```

## Step 5: Read the bootstrap prompt

The bootstrap prompt is your source of truth. It contains:

- **Governance summary**: deny rules, budgets, checks allowlist
- **Ticket scope**: allowed/denied file patterns
- **Session lifecycle commands**: how to finish
- **Operational learnings**: reflections from prior sessions
- **Tool registry**: reusable utilities already built
- **Sibling sessions**: other agents working in parallel
- **Start advisories**: scope conflicts, unmerged work, stale branches

Read the bootstrap before starting work:

```bash
cat .exo/cache/sessions/agent-claude.bootstrap.md
```

## Step 6: Do the work

While working, follow these rules:

### Respect ticket scope

Only modify files matching `scope.allow` patterns. Never touch files matching `scope.deny`.

```bash
# Check your current scope
exo ticket-show TKT-001
```

### Tag code with feature annotations

If `.exo/features.yaml` exists, wrap new code with feature tags:

```python
# @feature:auth-hashing
def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()
# @endfeature
```

### Search before building

Before writing a new utility function:

```bash
exo tool-search "hash password crypto"
```

If you build a reusable tool, register it:

```bash
exo tool-register src/auth/hash.py hash_password \
  --description "Hash plaintext password using bcrypt"
```

### Record learnings

When you discover a gotcha or pattern:

```bash
exo reflect \
  --pattern "bcrypt requires bytes input" \
  --insight "Always .encode() plaintext before bcrypt.hashpw()" \
  --severity medium
```

This makes the learning available to all future sessions.

## Step 7: Finish the session

When work is complete, finish the session:

```bash
EXO_ACTOR=agent:claude python3 -m exo.cli session-finish \
  --ticket-id TKT-001 \
  --summary "Implemented bcrypt-based password hashing with unit tests" \
  --set-status review
```

Or use the MCP tool:

```
exo_session_finish(
  ticket_id="TKT-001",
  summary="Implemented bcrypt-based password hashing with unit tests",
  set_status="review"
)
```

This triggers:
- **Checks**: runs commands from `.exo/config.yaml` checks_allowlist
- **Drift detection**: compares scope/budget usage to ticket limits
- **Feature trace**: verifies no uncovered code, no invalid tags
- **Requirement trace**: verifies all requirement references are valid
- **Coherence check**: detects missing co-updates, stale docstrings
- **Tool tracking**: records tools created/used this session
- **Follow-up generation**: creates tickets for governance gaps
- **Memento creation**: archives session history

If checks fail, fix the issue and run finish again. Do not skip checks without a break-glass reason.

## Non-negotiables

- **No work without session-start**: Always acquire a lock and start a session before editing code
- **No close without session-finish**: Always finish the session to release the lock and run checks
- **Respect ticket scope**: Only modify files allowed by `scope.allow`/`scope.deny`
- **Fix check failures**: If `session-finish` reports failed checks, fix them before closing
- **Never hardcode config values**: Load from `.exo/config.yaml` at runtime, write tests that vary config

## Common workflows

### Resuming a suspended session

If you suspended mid-work:

```bash
exo session-resume --ticket-id TKT-001
```

Read the updated bootstrap at `.exo/cache/sessions/agent-claude.bootstrap.md`.

### Running an audit session

To review another agent's work in adversarial mode:

```bash
exo session-audit \
  --ticket-id TKT-001 \
  --vendor anthropic \
  --model claude-code \
  --pr-base main \
  --pr-head feature-branch
```

Audit sessions:
- Deny access to `.exo/cache/**` and `.exo/memory/**`
- Inject adversarial Red Team directives
- Run PR governance checks
- Warn if auditor uses same model as writer

### Checking governance health

Before starting work, verify governance state:

```bash
exo drift
```

This checks:
- Governance integrity (constitution hash vs lock hash)
- Adapter freshness (CLAUDE.md etc. in sync with .exo/)
- Feature traceability
- Requirement coverage
- Semantic coherence
- Stale/orphaned sessions

### Cleaning up old sessions

Periodically run garbage collection:

```bash
exo gc --max-age-days 30
```

This removes:
- Old mementos
- Orphaned bootstraps
- Stale cursor files

## Troubleshooting

### Session start blocked

```
ExoError: Cannot start session without acquiring a ticket lock first
```

Solution: Use `exo next` to acquire a lock, or use `--ticket-id` with a ticket you already own.

### Scope violation at finish

```
drift_score: 0.85 (scope violations: 3)
```

Solution: Check `exo ticket-show <TKT>` for allowed patterns. Revert files outside scope before finishing.

### Check failures

```
checks_passed: false
failing_checks: ["pytest"]
```

Solution: Fix the failing tests, then run `session-finish` again.

### Uncovered code detected

```
uncovered_code violations: ["src/auth/new_file.py"]
```

Solution: Either:
- Add `@feature:<id>` tags around the code
- Add the file to an existing feature's `files` glob in `.exo/features.yaml`

## Further reading

- [Session Lifecycle](session-lifecycle.md) — suspend, resume, audit workflows
- [Governance Rules](governance-rules.md) — deny patterns, budgets, structural rules
- [MCP Tool Reference](mcp-tool-reference.md) — all 40+ MCP tools with examples
- [Error Reference](error-reference.md) — ExoError codes and remediation
- [Config Reference](config-reference.md) — `.exo/config.yaml` schema
