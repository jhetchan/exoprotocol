# (\/)(°,,°)(\/) [ExoProtocol](https://exoprotocol.dev)

[![PyPI](https://img.shields.io/pypi/v/exoprotocol)](https://pypi.org/project/exoprotocol/)

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

Session start generates a bootstrap prompt with governance rules, scope constraints, sibling awareness, and operational learnings from prior sessions. Session finish runs drift detection, feature tracing, and writes a closeout memento.

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

- [Architecture](https://exoprotocol.dev/#architecture) — layer model, key concepts, module map
- [exoprotocol.dev](https://exoprotocol.dev/) — project site

## License

MIT License. See [LICENSE](LICENSE).
