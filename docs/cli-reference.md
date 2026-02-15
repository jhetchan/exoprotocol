# CLI Reference

All commands support `--format json`.

## Core lifecycle

| Command | Description |
|---|---|
| `exo init [--seed] [--no-scan]` | Create .exo scaffold (scans repo by default) |
| `exo status` | Show ticket counts, active lock, dispatch candidate |
| `exo next [--owner] [--distributed]` | Dispatch next ticket and acquire lock |
| `exo do [TICKET-ID]` | Run controlled execution pipeline |
| `exo check` | Run allowlisted checks |
| `exo plan <input>` | Generate SPEC + tickets from input |

## Session lifecycle

| Command | Description |
|---|---|
| `exo session-start [--ticket-id] [--vendor] [--model] [--task]` | Start governed session with bootstrap |
| `exo session-finish --summary "..." [--set-status] [--error "tool:msg"]` | Finish session, write memento |
| `exo session-suspend --reason "..."` | Suspend session, release lock |
| `exo session-resume` | Resume suspended session |
| `exo session-audit [--ticket-id] [--pr-base] [--pr-head]` | Start audit session (adversarial review) |
| `exo session-scan [--stale-hours N]` | Scan for active/stale/orphaned sessions |
| `exo session-cleanup [--stale-hours N] [--release-lock]` | Clean up stale sessions |

## Governance and integrity

| Command | Description |
|---|---|
| `exo build-governance` | Compile constitution into governance lock |
| `exo audit` | Run integrity/rule/lock audit |
| `exo doctor [--stale-hours N]` | Unified health check (scaffold + config + drift + scan) |
| `exo config-validate` | Validate .exo/config.yaml structure and values |
| `exo drift [--skip-adapters] [--skip-features] [--skip-coherence] ...` | Composite governance drift check |
| `exo coherence [--skip-co-updates] [--skip-docstrings] [--base main]` | Check co-update rules and docstring freshness |
| `exo pr-check [--base] [--head] [--drift-threshold]` | PR governance check (commit-to-session coverage) |
| `exo upgrade [--dry-run]` | Upgrade .exo/ to latest schema (backfill config, create dirs) |

## Intent accountability

| Command | Description |
|---|---|
| `exo intent-create --brain-dump "..." [--boundary] [--success-condition]` | Create intent ticket |
| `exo ticket-create --title "..." [--kind task\|epic] [--parent]` | Create task/epic ticket |
| `exo intents [--status] [--drift-above N]` | List intents with timeline |
| `exo validate-hierarchy <ticket-id>` | Validate intent hierarchy |

## Feature and requirement traceability

| Command | Description |
|---|---|
| `exo features [--status active\|deprecated\|...]` | List features from manifest |
| `exo trace [--glob "..."]` | Scan code for @feature tags, report violations |
| `exo prune [--include-deprecated] [--dry-run]` | Remove deleted/deprecated feature code blocks |
| `exo requirements [--status]` | List requirements from manifest |
| `exo trace-reqs [--glob "..."]` | Scan code for @req/@implements tags |

## Tool awareness

| Command | Description |
|---|---|
| `exo tools [--tag TAG]` | List registered tools from .exo/tools.yaml |
| `exo tool-register <module> <function> --description "..."` | Register a reusable tool |
| `exo tool-search <query>` | Search tools by description/tags |
| `exo tool-remove <tool-id>` | Remove a tool from registry |
| `exo tool-use <tool-id> [--session-id]` | Record that a tool was used in a session |
| `exo tool-suggest` | Detect duplication patterns and suggest tool registration |

## Chain reaction

| Command | Description |
|---|---|
| `exo follow-ups [--ticket-id TID]` | Detect governance gaps that warrant follow-up tickets (dry-run) |

## Adapter generation

| Command | Description |
|---|---|
| `exo adapter-generate [--target claude\|cursor\|agents\|ci] [--dry-run]` | Generate agent config files from governance |
| `exo scan` | Preview what init would detect (read-only) |

## Error reflection and learning

| Command | Description |
|---|---|
| `exo reflect --pattern "..." --insight "..."` | Record operational learning |
| `exo reflections [--status] [--scope] [--severity]` | List stored reflections |
| `exo reflect-dismiss <REF-ID>` | Dismiss a reflection |

## Infrastructure

| Command | Description |
|---|---|
| `exo gc [--max-age-days N] [--dry-run]` | Garbage collect old mementos and caches |
| `exo gc-locks [--remote origin] [--dry-run] [--list]` | Clean up expired distributed leases |
| `exo sidecar-init [--branch] [--sidecar] [--remote]` | Mount .exo/ as dedicated governance worktree |

## Lease management

| Command | Description |
|---|---|
| `exo lease-renew [--ticket-id] [--hours N] [--distributed]` | Renew active ticket lease |
| `exo lease-heartbeat [--ticket-id] [--hours N] [--distributed]` | Heartbeat lease without token change |
| `exo lease-release [--ticket-id] [--distributed]` | Release active lease |

## Scratchpad and recall

| Command | Description |
|---|---|
| `exo jot "..."` | Append to scratchpad inbox |
| `exo thread "topic"` | Create scratchpad thread |
| `exo promote <thread-id> --to ticket` | Promote thread to ticket |
| `exo recall "query"` | Search local memory paths |

## Self-evolution

| Command | Description |
|---|---|
| `exo observe --ticket <id> --tag <tag> --msg "..."` | Record observation |
| `exo propose --ticket <id> --kind <kind> --symptom "..."` | Propose change |
| `exo approve <PROP-ID>` | Approve proposal |
| `exo apply <PROP-ID>` | Apply approved proposal |
| `exo distill <PROP-ID>` | Distill learnings to memory |

## Ledger and control plane

| Command | Description |
|---|---|
| `exo subscribe [--topic] [--since]` | Subscribe to ledger events |
| `exo read-ledger [ref-id] [--type] [--topic]` | Read ledger records |
| `exo head --topic <id>` | Inspect topic head pointer |
| `exo cas-head --topic <id> --cap <cap>` | Compare-and-swap topic head |
| `exo submit-intent --intent "..." [--topic]` | Submit intent to ledger |
| `exo check-intent <intent-id>` | Check intent decision |
| `exo begin-effect <decision-id> --executor-ref --idem-key` | Claim execution |
| `exo commit-effect <effect-id> --status OK\|FAIL` | Commit execution result |
| `exo ack <ref-id>` | Acknowledge a ledger ref |
| `exo quorum <ref-id> [--required N]` | Check quorum status |
| `exo decide-override <intent-id> --override-cap <cap>` | Override decision (cap-gated) |
| `exo policy-set --policy-cap <cap>` | Install policy bundle (cap-gated) |
| `exo worker-poll [--topic] [--since] [--limit N]` | Poll ledger topic once and execute pending intents |
| `exo worker-loop [--topic] [--iterations N] [--sleep-seconds N]` | Run repeated ledger polling loop |
| `exo escalate-intent <intent-id> --reason "..."` | Record escalation for an intent |

## MCP server

Install extra deps and run:

```bash
pip install -e .[mcp]
exo-mcp
```

### Example MCP configs

**Claude Desktop / Claude Code** (`~/.claude/claude_desktop_config.json`):

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

**Cursor** (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "exo": {
      "command": "exo-mcp",
      "args": [],
      "env": { "EXO_ACTOR": "agent:cursor" }
    }
  }
}
```

**Generic (any MCP-compatible client)**:

```json
{
  "mcpServers": {
    "exo": {
      "command": "python3",
      "args": ["-m", "exo.mcp_server"],
      "env": { "EXO_ACTOR": "agent:generic" }
    }
  }
}
```
