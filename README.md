# (\/)(°,,°)(\/) 
# [ExoProtocol](https://exoprotocol.dev)

[![PyPI](https://img.shields.io/pypi/v/exoprotocol)](https://pypi.org/project/exoprotocol/)
[![Python](https://img.shields.io/badge/python-≥3.10-blue)](https://pypi.org/project/exoprotocol/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Governance kernel for multi-agent development. Treats AI-agent sessions the way an OS treats processes — with enforceable scope, lifecycle control, audit trails, and crash recovery. Everything lives in your git repo.

## Why

When multiple AI agents vibe-code on the same repo, things go wrong silently: scope collisions, forgotten context, ungoverned commits, duplicated work. ExoProtocol gives each agent session a ticket, a scope fence, and a paper trail — without leaving git.

## Install

```bash
pip install exoprotocol
```

## Quickstart

```bash
# 1. Initialize governance (scans your repo, generates rules + adapters)
exo init

# 2. Health check
exo doctor

# 3. Dispatch a ticket
exo next --owner your-name

# 4. Start a governed session
EXO_ACTOR=agent:claude exo session-start \
  --ticket-id TICKET-001 \
  --vendor anthropic --model claude-code \
  --context-window 200000 \
  --task "implement the feature described in the ticket"

# 5. Finish the session
EXO_ACTOR=agent:claude exo session-finish \
  --summary "implemented feature, added tests" \
  --set-status review
```

Session start generates a bootstrap prompt with governance rules, scope constraints, sibling awareness, and operational learnings from prior sessions. Session finish runs drift detection (scope compliance, file budget, boundary violations), feature tracing, and writes a closeout memento.

## Agent handoff

```bash
# Agent A hands off to Agent B
EXO_ACTOR=agent:claude-opus exo session-handoff \
  --to agent:claude-sonnet --ticket-id TICKET-001 \
  --summary "Built API endpoints" --next-step "Write tests"

# Agent B starts — handoff context auto-injected into bootstrap
EXO_ACTOR=agent:claude-sonnet exo session-start --ticket-id TICKET-001 ...
```

## Claude Code hooks

```bash
exo hook-install --all   # session lifecycle + enforcement + git pre-commit
```

Installs all six hook types:
- **SessionStart/SessionEnd** — auto-start/finish governed sessions with bootstrap injection
- **PreToolUse (Bash)** — gate `git commit`/`git push` on `exo check`
- **PreToolUse (Write|Edit)** — block writes outside ticket scope (real enforcement, not advisory)
- **PostToolUse (Write|Edit)** — auto-format Python files + budget tracking with warnings
- **Notification** — audit trail logging to `.exo/audit/notifications.jsonl`
- **Stop** — session hygiene warning before agent stops
- **Git pre-commit** — runs `exo check` before every commit

Parallel instances get unique actor IDs via `CLAUDE_ENV_FILE`, so multiple Claude Code windows can work on different tickets without session clashes. Auto-branch creates `exo/<ticket-id>` branches on session-start.

## Agent adapters

```bash
exo adapter-generate                    # all targets
exo adapter-generate --target codex     # OpenAI Codex
exo adapter-generate --target claude    # CLAUDE.md
exo adapter-generate --target ci        # GitHub Actions workflow
```

Generates governance-aware config for Claude Code (`CLAUDE.md`), Cursor (`.cursorrules`), `AGENTS.md`, OpenAI Codex (`codex.md`), and CI (`.github/workflows/exo-governance.yml`). All adapters reflect the current governance state — deny patterns, budgets, checks, lifecycle commands, and active intent provenance with scope boundaries.

## SDK integrations

```python
# OpenAI Agents SDK
from exo.integrations.openai_agents import ExoRunHooks
hooks = ExoRunHooks(repo=".", ticket_id="TKT-...", actor="agent:openai")
result = await Runner.run(agent, hooks=hooks)
```

```bash
pip install exoprotocol[openai-agents]  # OpenAI Agents SDK
pip install exoprotocol[claude]         # Claude Code hooks (auto-installed)
```

## What session-start does

When an agent session starts, ExoProtocol:

1. Compiles governance rules from the repo constitution
2. Loads the ticket's scope, budget, and constraints
3. Scans for sibling sessions and warns about scope conflicts
4. Checks for unmerged work on other branches that overlaps your scope
5. Injects operational learnings from prior sessions
6. Produces a bootstrap prompt that the agent sees first

The agent works within these boundaries. At session-finish, drift detection scores how well the work stayed in scope.

## Architecture

```
CLI / MCP  →  Orchestrator  →  Stdlib  →  Control  →  Kernel (frozen, 10 functions)
```

The kernel is intentionally small and frozen — governance compilation, ticket locks, audit log, rule checks. Everything else (session lifecycle, drift detection, feature tracing, dispatch, GC) lives in the stdlib. See [docs/architecture.md](docs/architecture.md).

## MCP server

```bash
pip install exoprotocol[mcp]
exo-mcp
```

```json
{
  "mcpServers": {
    "exo": {
      "command": "exo-mcp",
      "args": [],
      "env": { "EXO_ACTOR": "agent:claude" }
    }
  }
}
```

Works with Claude Code, Cursor, and any MCP-compatible client. Every CLI command has a matching MCP tool.

## Documentation

### Guides (for humans)

- [Getting Started](docs/guides/quickstart.md) — install, init, connect your agents
- [Workflow Patterns](docs/guides/workflows.md) — solo, multi-agent, code review, CI
- [FAQ](docs/guides/faq.md) — common questions and quick fixes

### Agent Reference

- [Agent Quickstart](docs/agents/quickstart.md) — zero to first governed session
- [Session Lifecycle](docs/agents/session-lifecycle.md) — state machine, bootstrap anatomy, handoff, audit mode
- [Governance Rules](docs/agents/governance-rules.md) — rule types, budgets, scope, traceability
- [Config Reference](docs/agents/config-reference.md) — complete `.exo/config.yaml` schema
- [Error Reference](docs/agents/error-reference.md) — every ExoError code with resolution steps
- [MCP Tool Reference](docs/agents/mcp-tool-reference.md) — all 68 MCP tool signatures

### Shared

- [CLI Reference](docs/cli-reference.md) — all commands at a glance
- [Architecture](docs/architecture.md) — layer model, key concepts, module map
- [exoprotocol.dev](https://exoprotocol.dev/) — project site

## License

MIT License. See [LICENSE](LICENSE).
