# Getting Started

## What is ExoProtocol?

ExoProtocol is a governance kernel that lives in your git repo. It treats AI agent sessions the way an OS treats processes -- with scope fences, lifecycle control, audit trails, and crash recovery. You install it once, and every agent that touches your repo works within boundaries you control.

If you use Claude Code, Cursor, Copilot, or any MCP-capable agent to write code, ExoProtocol gives you guardrails without heavy process. No dashboards, no SaaS, no config servers. Just files in your repo that your agents read and respect.

## Install

```bash
pip install exoprotocol
```

That's it. One package, no system dependencies beyond Python 3.10+.

If you plan to connect agents via MCP (you probably do), grab the MCP extra too:

```bash
pip install exoprotocol[mcp]
```

## Initialize your repo

Before you initialize, you can preview what ExoProtocol detects about your project:

```bash
exo scan
```

This is read-only -- it just prints what it finds (languages, sensitive files, CI systems, source directories) without creating anything.

When you're ready, the fastest path is the one-shot setup:

```bash
exo install
```

This runs the full setup pipeline in one command:

1. **Init** -- creates the `.exo/` directory with a constitution, config, and compiled governance lock. Scans your repo to detect languages, sensitive files, and CI systems.
2. **Compile** -- compiles the constitution into a tamper-evident governance lock.
3. **Adapters** -- generates `CLAUDE.md`, `.cursorrules`, `AGENTS.md`, and CI workflow. If these files already exist, ExoProtocol merges its governance section in without clobbering your content.
4. **Hooks** -- installs Claude Code hooks (session lifecycle, scope enforcement, git pre-commit).
5. **Gitignore** -- creates `.exo/.gitignore` to exclude ephemeral data (caches, logs, locks) while keeping governance state committed.

The whole thing is idempotent. Running `exo install` again refreshes governance state without overwriting your customizations. Use `--skip-init`, `--skip-hooks`, or `--skip-adapters` to skip individual steps.

> **Tip:** If you prefer step-by-step control, you can still run `exo init`, `exo build-governance`, `exo adapter-generate`, and `exo hook-install` individually.

## Health check

```bash
exo doctor
```

This runs a quick diagnostic across your governance setup:

- Is the `.exo/` scaffold complete?
- Is the config valid?
- Are the governance rules consistent (constitution matches lock)?
- Are agent adapter files up to date?
- Are there any stale or orphaned sessions?

If something is off, `exo doctor` tells you what and how to fix it. Think of it as `git status` for your governance layer.

## Connect your agents

ExoProtocol talks to agents through MCP (Model Context Protocol). Each agent gets an actor identity so the audit trail knows who did what.

### Claude Code

Add this to your `~/.claude.json` (or project-level `.claude/settings.json`):

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

### Cursor

Create `.cursor/mcp.json` in your project root:

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

### Other MCP clients

Any MCP-capable client can connect using the module entry point:

```json
{
  "mcpServers": {
    "exo": {
      "command": "python3",
      "args": ["-m", "exo.mcp_server"],
      "env": { "EXO_ACTOR": "agent:your-agent-name" }
    }
  }
}
```

The `EXO_ACTOR` value is how ExoProtocol identifies who's working. Use a consistent name per agent so session history makes sense.

## What you get

Once ExoProtocol is initialized and your agents are connected, here's what's working for you:

- **Scope fences** -- Each ticket defines what files an agent can touch. If an agent wanders outside its lane, the violation is flagged at session-finish and in PR checks.
- **Session lifecycle** -- Every agent session has a start, a finish, and a paper trail (called a memento). You always know what happened, when, and by whom.
- **Sibling awareness** -- When multiple agents work on the same repo simultaneously, each one sees what the others are doing in its bootstrap prompt. No more two agents editing the same file.
- **Drift detection** -- At session-finish, ExoProtocol scores how well the work stayed within scope, file budgets, and LOC budgets. High drift gets flagged.
- **Crash recovery** -- If an agent dies mid-session, `exo session-cleanup` detects the orphan (via PID liveness checks) and recovers gracefully.
- **Audit mode** -- Review another agent's work with `exo session-audit`. It boots a fresh session with context isolation and an adversarial review persona. Pair it with `--pr-base main` to get a full PR governance report.
- **CI enforcement** -- The generated GitHub Actions workflow runs `exo pr-check` on every pull request, verifying that commits trace back to governed sessions.

## Your first governed workflow

Here's the simplest end-to-end flow. You don't need to memorize this -- your agent sees these instructions in its bootstrap prompt automatically.

**1. Plan the work**

```bash
exo plan "add login page"
```

This creates tickets with scope, budgets, and IDs. For bigger efforts, use `exo intent-create` to define a high-level intent first, then break it into tasks.

**2. See what was created**

```bash
exo status
```

You'll see your tickets with their IDs, scopes, and statuses.

**3. Your agent starts working**

When your agent (Claude Code, Cursor, etc.) begins a session, ExoProtocol injects governance context into its prompt. The agent sees the rules, the ticket scope, any sibling sessions, and operational learnings from past sessions. It works within the boundaries you defined.

**4. The agent finishes**

At session-finish, ExoProtocol automatically:
- Writes a memento (session summary with file changes, drift score, warnings)
- Runs drift detection, feature tracing, and coherence checks (all advisory)
- Detects if the agent wrote to private memory instead of shared reflections
- Creates follow-up tickets for any governance gaps it found

**5. You review and merge**

Check the memento, review the diff, open a PR. The CI workflow runs `exo pr-check` and reports whether every commit was governed, scoped correctly, and within drift thresholds.

That's the whole loop. Plan, work, finish, review.

## What's next?

- [Workflow patterns](workflows.md) -- solo, multi-agent, code review, CI
- [FAQ](faq.md) -- common questions and fixes
- [CLI Reference](../cli-reference.md) -- all commands
- [Architecture](../architecture.md) -- how the layers work
