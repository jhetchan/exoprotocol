"""Adapter generation: produce repo-root agent config files from .exo/ governance state.

Generates CLAUDE.md, .cursorrules, and AGENTS.md that agent runtimes auto-read,
bridging ExoProtocol governance into the agent's native config format.
"""
# @feature:adapter-generation
# @req: REQ-ADAPT-001

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from exo.kernel.errors import ExoError
from exo.kernel.utils import load_json, load_yaml, now_iso

GOVERNANCE_LOCK_PATH = Path(".exo/governance.lock.json")
CONFIG_PATH = Path(".exo/config.yaml")

# Marker delimiters for brownfield-safe adapter generation.
# Content between these markers is exo-managed; everything outside is user content.
EXO_MARKER_BEGIN = "<!-- exo:governance:begin -->"
EXO_MARKER_END = "<!-- exo:governance:end -->"

ADAPTER_TARGETS = frozenset({"claude", "cursor", "agents", "ci", "sandbox"})
AGENT_ADAPTER_TARGETS = frozenset({"claude", "cursor", "agents"})  # targets with governance preamble

# Map target name → output file
TARGET_FILES: dict[str, str] = {
    "claude": "CLAUDE.md",
    "cursor": ".cursorrules",
    "agents": "AGENTS.md",
    "ci": ".github/workflows/exo-governance.yml",
    "sandbox": ".claude/settings.json",
}


def _load_governance_lock(repo: Path) -> dict[str, Any]:
    lock_path = repo / GOVERNANCE_LOCK_PATH
    if not lock_path.exists():
        raise ExoError(
            code="GOVERNANCE_LOCK_MISSING",
            message="governance.lock.json not found — run `exo build-governance` first",
            blocked=True,
        )
    return load_json(lock_path)


def _load_config(repo: Path) -> dict[str, Any]:
    config_path = repo / CONFIG_PATH
    if not config_path.exists():
        return {}
    return load_yaml(config_path)


def _format_deny_rules(rules: list[dict[str, Any]]) -> list[str]:
    """Extract filesystem_deny rules into human-readable lines."""
    lines: list[str] = []
    for rule in rules:
        if rule.get("type") != "filesystem_deny":
            continue
        rule_id = rule.get("id", "")
        patterns = rule.get("patterns", [])
        actions = rule.get("actions", [])
        lines.append(f"- **{rule_id}**: deny {', '.join(actions)} on `{'`, `'.join(patterns)}`")
    return lines


def _format_structural_rules(rules: list[dict[str, Any]]) -> list[str]:
    """Extract non-filesystem rules into human-readable lines."""
    lines: list[str] = []
    for rule in rules:
        rtype = rule.get("type", "")
        if rtype == "filesystem_deny":
            continue
        rule_id = rule.get("id", "")
        msg = rule.get("message", "")
        lines.append(f"- **{rule_id}** ({rtype}): {msg}")
    return lines


def _generate_preamble(lock: dict[str, Any], config: dict[str, Any], repo: Path | None = None) -> str:
    """Shared governance preamble used by all adapter targets."""
    kernel = lock.get("kernel", {})
    rules = lock.get("rules", [])

    deny_lines = _format_deny_rules(rules)
    structural_lines = _format_structural_rules(rules)

    checks_allowlist = config.get("checks_allowlist", [])
    budgets = config.get("defaults", {}).get("ticket_budgets", {})

    sections = [
        "## ExoProtocol Governance",
        "",
        f"- kernel: {kernel.get('name', 'exo-kernel')} {kernel.get('version', '')}",
        f"- lock hash: `{lock.get('source_hash', 'unknown')[:16]}...`",
        f"- generated: {lock.get('generated_at', '')}",
        "",
        "### Filesystem Deny Rules",
        "",
    ]
    sections.extend(deny_lines or ["- (none)"])
    sections.extend(
        [
            "",
            "### Structural Rules",
            "",
        ]
    )
    sections.extend(structural_lines or ["- (none)"])

    if budgets:
        sections.extend(
            [
                "",
                "### Default Budgets",
                "",
                f"- max files changed: {budgets.get('max_files_changed', 12)}",
                f"- max LOC changed: {budgets.get('max_loc_changed', 400)}",
            ]
        )

    if checks_allowlist:
        sections.extend(
            [
                "",
                "### Approved Checks",
                "",
            ]
        )
        for cmd in checks_allowlist:
            sections.append(f"- `{cmd}`")

    # Manifest-driven workflow directive
    sections.extend(
        [
            "",
            "### Source of Truth",
            "",
            "The values above are a **snapshot** generated from the governance manifest.",
            "",
            "Manifest paths:",
            "- `.exo/config.yaml` — budgets, checks allowlist, scheduler config",
            "- `.exo/governance.lock.json` — compiled rules, deny patterns, source hash",
            "",
            "### Test-Driven, Manifest-First Workflow",
            "",
            "This principle applies to **all code you write** — governance and application logic alike.",
            "",
            "1. **Config/contract is the source of truth.** When a value is defined in a config file,",
            "   schema, manifest, or contract — code must load it from that source at runtime.",
            "   Never copy a value from a config file and paste it as a literal in source code.",
            "2. **Tests verify the wiring, not the value.** Tests must assert that code reads from",
            "   the config/contract, not that it produces a specific hardcoded result.",
            "   A test that passes when you swap the config value *and* swap the assertion is useless —",
            "   it only proves both sides were copy-pasted from the same place.",
            "3. **If you can change a config value and no test breaks, the test is missing.**",
            "   Every configurable value should have at least one test that will vary the input",
            "   and verify the output follows.",
            "",
            "Examples:",
            "- **BAD**: `assert budget == 10` (hardcoded, passes even if config is ignored)",
            "- **GOOD**: set config to 42, assert output contains 42 and not the old default",
            "- **BAD**: `MAX_RETRIES = 3` (literal in source when retries is in config)",
            "- **GOOD**: `max_retries = load_config()['max_retries']`",
            "",
            "### Operational Learnings",
            "",
            "When you discover a reusable pattern, gotcha, or operational insight during a session:",
            "- Record it with `exo reflect` (CLI) or `exo_reflect` (MCP) — NOT your private memory",
            "- ExoProtocol reflections are injected into future session bootstraps for all agents",
            "- Private memory files (MEMORY.md, .cursorrules, etc.) are agent-specific and invisible to the team",
            "- If you must write to private memory, also create an ExoProtocol reflection with the same insight",
            "",
            "**Private memory monitoring**: If `private_memory.watch_paths` in `.exo/config.yaml` is empty,",
            "add the absolute path to your memory file (e.g., `~/.claude/.../memory/MEMORY.md`) so that",
            "ExoProtocol can detect when you write to private memory without creating a shared reflection.",
            "",
            "### End-of-Work Reflection",
            "",
            "When you complete significant work or the user appears to be wrapping up:",
            "- **Proactively** run `exo reflect --pattern '<what kept happening>' --insight '<what was learned>'`",
            "  for each non-trivial insight discovered during the conversation",
            "- Do NOT wait for `session-finish` — many users close the editor without explicit session end",
            "- Good reflection triggers: bug fixes, CI failures, gotchas, architectural decisions, workflow improvements",
        ]
    )

    # Feature Governance Protocol (advisory — skipped if features.yaml missing)
    if repo is not None:
        try:
            features_path = repo / Path(".exo/features.yaml")
            if features_path.exists():
                sections.extend(
                    [
                        "",
                        "### Feature Governance Protocol",
                        "",
                        "All source code must be governed by the feature manifest (`.exo/features.yaml`).",
                        "",
                        "Before writing code:",
                        "1. Check which feature your work belongs to: `exo features`",
                        "2. Add `@feature:<feature-id>` / `@endfeature` tags around new code blocks",
                        "",
                        "After finishing:",
                        "- Run `exo trace` to verify no uncovered code",
                        "- If you built a new subsystem, add it to `.exo/features.yaml` first",
                    ]
                )
        except Exception:  # noqa: BLE001
            pass  # Advisory — never blocks adapter generation

    # Tool Reuse Protocol (advisory — skipped if tools module unavailable)
    if repo is not None:
        try:
            from exo.stdlib.tools import TOOLS_PATH, load_tools

            tools = load_tools(repo) if (repo / TOOLS_PATH).exists() else []
            sections.extend(
                [
                    "",
                    "### Tool Reuse Protocol",
                    "",
                    "Before writing new utility functions, SEARCH the tool registry:",
                    '  `exo tool-search "<keywords>"`',
                    "",
                    "After building a reusable utility, REGISTER it:",
                    '  `exo tool-register <module> <function> --description "..."`',
                    "",
                    "Mark a tool as used when you import/call it:",
                    "  `exo tool-use <tool_id>`",
                    "",
                ]
            )
            if tools:
                sections.append(f"**Registered Tools ({len(tools)}):**")
                for tool in tools:
                    tag_str = f" [{', '.join(tool.tags)}]" if tool.tags else ""
                    sections.append(f"- `{tool.id}`{tag_str}: {tool.description}")
                sections.append("")
        except Exception:  # noqa: BLE001
            pass  # Advisory — never blocks adapter generation

    return "\n".join(sections)


def generate_claude(repo: Path, lock: dict[str, Any], config: dict[str, Any]) -> str:
    """Generate CLAUDE.md content for Claude Code."""
    preamble = _generate_preamble(lock, config, repo=repo)

    return f"""\
# ExoProtocol — Governed Repository

This repository uses ExoProtocol governance. All work must go through the session lifecycle.

{preamble}

## Session Lifecycle

Before starting any work:

1. **Start session**: `EXO_ACTOR=agent:claude python3 -m exo.cli session-start --ticket-id <TICKET> --vendor anthropic --model <MODEL> --task "<TASK>"`
2. **Read bootstrap**: Open `.exo/cache/sessions/agent-claude.bootstrap.md` and follow its directives
3. **Execute work** within ticket scope
4. **Finish session**: `EXO_ACTOR=agent:claude python3 -m exo.cli session-finish --ticket-id <TICKET> --summary "<SUMMARY>" --set-status review`

## Non-Negotiables

- Do NOT start work without an active session (`session-start`)
- Do NOT close without a session finish (`session-finish`)
- Respect ticket scope: only modify files allowed by the ticket's `scope.allow` / `scope.deny`
- If checks fail at finish, fix them — do not use `--skip-check` without `--break-glass-reason`
- The bootstrap file is your source of truth for the current session
- Never hardcode values that belong in config — load from manifest at runtime, write tests that vary the config
- Read `.exo/LEARNINGS.md` for operational learnings from prior sessions
"""


def generate_cursor(repo: Path, lock: dict[str, Any], config: dict[str, Any]) -> str:
    """Generate .cursorrules content for Cursor IDE."""
    preamble = _generate_preamble(lock, config, repo=repo)

    return f"""\
# ExoProtocol — Governed Repository

This repository uses ExoProtocol governance. All work must go through the session lifecycle.

{preamble}

## Session Lifecycle

Before starting any work:

1. Start session: `EXO_ACTOR=agent:cursor python3 -m exo.cli session-start --ticket-id <TICKET> --vendor cursor --model <MODEL> --task "<TASK>"`
2. Read bootstrap: `.exo/cache/sessions/agent-cursor.bootstrap.md`
3. Execute work within ticket scope
4. Finish session: `EXO_ACTOR=agent:cursor python3 -m exo.cli session-finish --ticket-id <TICKET> --summary "<SUMMARY>" --set-status review`

## Rules

- No work without active session
- Respect ticket scope (allow/deny globs)
- Finish with summary and memento
- Skip-check requires break-glass reason
- Never hardcode configurable values — load from manifest/config, write tests that vary inputs
- Read `.exo/LEARNINGS.md` for operational learnings from prior sessions
"""


def generate_agents(repo: Path, lock: dict[str, Any], config: dict[str, Any]) -> str:
    """Generate AGENTS.md content (vendor-agnostic, for Copilot/generic runtimes)."""
    preamble = _generate_preamble(lock, config, repo=repo)

    return f"""\
# ExoProtocol — Agent Operating Instructions

This repository is governed by ExoProtocol. All AI agent work must follow the session lifecycle.

{preamble}

## Session Lifecycle

1. `exo session-start --ticket-id <TICKET> --vendor <VENDOR> --model <MODEL> --task "<TASK>"`
2. Read `.exo/cache/sessions/<actor>.bootstrap.md`
3. Execute work within ticket scope
4. `exo session-finish --ticket-id <TICKET> --summary "<SUMMARY>" --set-status review`

## Enforcement

- Governance rules are enforced at the kernel level, not by prompt
- The bootstrap file contains your session's scope, checks, and lifecycle commands
- Drift detection runs at session-finish and is recorded in the session memento
- Audit sessions may be triggered to review your work independently

## Non-Negotiables

- No governed execution without active session
- Respect lock ownership and ticket scope
- Verification is default at finish; break-glass must be explicit
- All configurable values must be loaded from their source of truth at runtime — never hardcode, always test
- Read `.exo/LEARNINGS.md` for operational learnings from prior sessions
"""


def generate_ci(repo: Path, lock: dict[str, Any], config: dict[str, Any]) -> str:
    """Generate GitHub Action workflow for PR governance checks.

    Reads drift_threshold and python_version from config.ci section.
    """
    ci_config = config.get("ci", {})
    drift_threshold = ci_config.get("drift_threshold", 0.7)
    python_version = str(ci_config.get("python_version", "3.11"))
    install_cmd = ci_config.get("install_command", "pip install -e .")
    governance_hash = lock.get("source_hash", "unknown")

    checks_allowlist = config.get("checks_allowlist", [])
    checks_step = ""
    if checks_allowlist:
        checks_cmds = "\n".join(f"          {cmd}" for cmd in checks_allowlist)
        checks_step = f"""
      - name: Run governed checks
        run: |
{checks_cmds}
"""

    return f"""\
# ExoProtocol Governance Check
# Auto-generated by: exo adapter-generate --target ci
# Governance hash: {governance_hash[:16]}...
#
# This workflow runs `exo pr-check` on every pull request to verify that
# all commits are covered by governed sessions, scope is respected, and
# drift scores are within threshold.

name: ExoProtocol Governance

on:
  pull_request:
    branches: [main]

permissions:
  contents: read
  pull-requests: read

jobs:
  governance-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: "{python_version}"

      - name: Install ExoProtocol
        run: {install_cmd}

      - name: PR governance check
        run: |
          python3 -m exo.cli pr-check \\
            --base ${{{{ github.event.pull_request.base.sha }}}} \\
            --head ${{{{ github.event.pull_request.head.sha }}}} \\
            --drift-threshold {drift_threshold}

      - name: Save governance report
        if: always()
        run: |
          python3 -m exo.cli --format json pr-check \\
            --base ${{{{ github.event.pull_request.base.sha }}}} \\
            --head ${{{{ github.event.pull_request.head.sha }}}} \\
            --drift-threshold {drift_threshold} \\
            > exo-governance-report.json || true

      - name: Upload governance report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: exo-governance-report
          path: exo-governance-report.json
          retention-days: 30
{checks_step}"""


def _derive_permission_deny(rules: list[dict[str, Any]], repo: Path | None = None) -> list[str]:
    """Convert constitution filesystem_deny rules to Claude Code permission deny entries.

    Maps governance deny rules to the Claude Code permissions format:
    - read action → Read(pattern)
    - write action → Edit(pattern)
    - delete action → Edit(pattern) + Bash(rm pattern)
    """
    entries: list[str] = []
    for rule in rules:
        if rule.get("type") != "filesystem_deny":
            continue
        patterns = rule.get("patterns", [])
        actions = rule.get("actions", [])
        for pattern in patterns:
            if "read" in actions:
                entries.append(f"Read({pattern})")
            if "write" in actions:
                entries.append(f"Edit({pattern})")
            if "delete" in actions:
                if "write" not in actions:
                    entries.append(f"Edit({pattern})")
                entries.append(f"Bash(rm {pattern})")

    # Feature governance: locked features generate deny globs
    if repo is not None:
        try:
            from exo.stdlib.features import generate_scope_deny, load_features

            features = load_features(repo)
            feature_deny = generate_scope_deny(features)
            for pattern in feature_deny:
                entries.append(f"Edit({pattern})")
        except Exception:  # noqa: BLE001
            pass  # Advisory — never blocks adapter generation

    return entries


def generate_sandbox(repo: Path, lock: dict[str, Any], config: dict[str, Any]) -> str:
    """Generate .claude/settings.json permissions fragment from governance state.

    Returns JSON string with permissions.deny derived from constitution deny rules.
    The caller merges this into existing settings.json to preserve hooks/other config.
    """
    rules = lock.get("rules", [])
    deny_entries = _derive_permission_deny(rules, repo=repo)
    governance_hash = lock.get("source_hash", "")

    settings_fragment: dict[str, Any] = {
        "permissions": {
            "deny": deny_entries,
        },
        "_exo_governance_hash": governance_hash[:16],
    }
    return json.dumps(settings_fragment, indent=2, ensure_ascii=True)


_GENERATORS: dict[str, Any] = {
    "claude": generate_claude,
    "cursor": generate_cursor,
    "agents": generate_agents,
    "ci": generate_ci,
    "sandbox": generate_sandbox,
}


def derive_sandbox_policy(repo: Path) -> dict[str, Any]:
    """Derive sandbox permission policy from constitution deny rules.

    Public API for inspecting what sandbox permissions would be generated
    without writing any files. Useful for previewing or programmatic access.

    Returns dict with deny_entries, source_rules, and governance_hash.
    """
    repo = Path(repo).resolve()
    lock = _load_governance_lock(repo)
    rules = lock.get("rules", [])
    deny_entries = _derive_permission_deny(rules, repo=repo)

    # Collect source rule IDs for traceability
    source_rules: list[dict[str, str]] = []
    for rule in rules:
        if rule.get("type") != "filesystem_deny":
            continue
        source_rules.append(
            {
                "id": rule.get("id", ""),
                "patterns": rule.get("patterns", []),
                "actions": rule.get("actions", []),
            }
        )

    return {
        "deny_entries": deny_entries,
        "deny_count": len(deny_entries),
        "source_rules": source_rules,
        "source_rule_count": len(source_rules),
        "governance_hash": lock.get("source_hash", "")[:16],
    }


def format_sandbox_policy_human(policy: dict[str, Any]) -> str:
    """Format sandbox policy for human-readable CLI output."""
    lines: list[str] = []
    lines.append(f"Governance hash: {policy.get('governance_hash', 'unknown')}")
    lines.append(f"Source deny rules: {policy.get('source_rule_count', 0)}")
    lines.append("")

    source_rules = policy.get("source_rules", [])
    if source_rules:
        lines.append("Constitution deny rules:")
        for sr in source_rules:
            rule_id = sr.get("id", "?")
            patterns = ", ".join(sr.get("patterns", []))
            actions = ", ".join(sr.get("actions", []))
            lines.append(f"  {rule_id}: {actions} on {patterns}")
        lines.append("")

    deny_entries = policy.get("deny_entries", [])
    lines.append(f"Derived sandbox permissions.deny ({len(deny_entries)} entries):")
    if deny_entries:
        for entry in deny_entries:
            lines.append(f"  - {entry}")
    else:
        lines.append("  (none)")

    return "\n".join(lines)


# ── Brownfield Merge Helpers ────────────────────────────────────


def _wrap_with_markers(content: str, governance_hash: str) -> str:
    """Wrap generated content in exo governance markers with embedded hash."""
    hash_comment = f"<!-- Governance hash: {governance_hash[:16]} -->"
    return f"{EXO_MARKER_BEGIN}\n{hash_comment}\n{content}\n{EXO_MARKER_END}\n"


_MARKER_RE = re.compile(
    re.escape(EXO_MARKER_BEGIN) + r".*?" + re.escape(EXO_MARKER_END) + r"\n?",
    re.DOTALL,
)


def _extract_marker_sections(existing: str) -> tuple[str, str, str] | None:
    """Split file around exo governance markers.

    Returns (before, governed, after) or None if no markers found.
    """
    match = _MARKER_RE.search(existing)
    if not match:
        return None
    before = existing[: match.start()]
    governed = match.group(0)
    after = existing[match.end() :]
    return (before, governed, after)


def _merge_with_existing(existing: str, new_governed: str, governance_hash: str) -> tuple[str, bool]:
    """Merge new governance content into an existing file.

    Returns (merged_content, had_markers).
    If existing has markers → replace governed section (had_markers=True).
    If no markers → append governed section at end (had_markers=False).
    """
    sections = _extract_marker_sections(existing)
    wrapped = _wrap_with_markers(new_governed, governance_hash)

    if sections is not None:
        before, _old_governed, after = sections
        return (before + wrapped + after, True)

    # No markers: preserve existing content, append governed section
    separator = "\n" if existing and not existing.endswith("\n") else ""
    return (existing + separator + "\n" + wrapped, False)


def _count_user_lines(content: str) -> int:
    """Count non-blank lines outside exo governance markers."""
    sections = _extract_marker_sections(content)
    if sections is None:
        # No markers — all content is user content
        return sum(1 for line in content.splitlines() if line.strip())

    before, _governed, after = sections
    user_text = before + after
    return sum(1 for line in user_text.splitlines() if line.strip())


def generate_adapters(
    repo: Path,
    *,
    targets: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Generate repo-root adapter files from .exo/ governance state.

    Args:
        repo: Repository root path.
        targets: List of target names (claude, cursor, agents). None = all.
        dry_run: If True, return content without writing files.

    Returns:
        Dict with generated file paths and content.
    """
    repo = Path(repo).resolve()

    chosen = targets if targets else sorted(ADAPTER_TARGETS)
    invalid = [t for t in chosen if t not in ADAPTER_TARGETS]
    if invalid:
        raise ExoError(
            code="ADAPTER_TARGET_INVALID",
            message=f"unknown adapter target(s): {', '.join(invalid)}",
            details={"valid_targets": sorted(ADAPTER_TARGETS), "invalid": invalid},
            blocked=True,
        )

    lock = _load_governance_lock(repo)
    config = _load_config(repo)
    governance_hash = lock.get("source_hash", "")

    results: dict[str, Any] = {}
    written: list[str] = []

    for target in chosen:
        generator = _GENERATORS[target]
        content = generator(repo, lock, config)
        filename = TARGET_FILES[target]
        output_path = repo / filename

        result_entry: dict[str, Any] = {
            "file": filename,
            "path": str(output_path.relative_to(repo)),
        }

        is_agent_target = target in AGENT_ADAPTER_TARGETS

        if is_agent_target:
            # Brownfield merge for agent adapter targets
            existing = ""
            file_exists = output_path.exists()
            if file_exists:
                existing = output_path.read_text(encoding="utf-8")

            if file_exists and existing:
                merged, had_markers = _merge_with_existing(existing, content, governance_hash)
                backed_up = False
                if not had_markers and not dry_run:
                    # First brownfield merge — backup original
                    backup_path = output_path.parent / f"{output_path.name}.pre-exo"
                    if not backup_path.exists():
                        shutil.copy2(output_path, backup_path)
                        backed_up = True

                result_entry["content_length"] = len(merged)
                result_entry["merged"] = True
                result_entry["had_markers"] = had_markers
                result_entry["user_lines"] = _count_user_lines(merged)
                result_entry["backed_up"] = backed_up

                if not dry_run:
                    output_path.write_text(merged, encoding="utf-8")
                    written.append(filename)
                else:
                    result_entry["content"] = merged
            else:
                # No existing file — wrap in markers and write
                wrapped = _wrap_with_markers(content, governance_hash)
                result_entry["content_length"] = len(wrapped)
                result_entry["merged"] = False
                result_entry["had_markers"] = False
                result_entry["user_lines"] = 0
                result_entry["backed_up"] = False

                if not dry_run:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(wrapped, encoding="utf-8")
                    written.append(filename)
                else:
                    result_entry["content"] = wrapped
        elif target == "sandbox":
            # Sandbox target: JSON merge with existing .claude/settings.json
            fragment = json.loads(content)
            existing_settings: dict[str, Any] = {}
            if output_path.exists():
                try:
                    existing_settings = json.loads(output_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    existing_settings = {}

            existing_settings["permissions"] = fragment["permissions"]
            existing_settings["_exo_governance_hash"] = fragment["_exo_governance_hash"]
            merged_json = json.dumps(existing_settings, indent=2, ensure_ascii=True) + "\n"

            result_entry["content_length"] = len(merged_json)
            result_entry["merged"] = bool(existing_settings.keys() - {"permissions", "_exo_governance_hash"})
            if not dry_run:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(merged_json, encoding="utf-8")
                written.append(filename)
            else:
                result_entry["content"] = merged_json
        else:
            # CI target: unconditional overwrite (no markers, no merge)
            result_entry["content_length"] = len(content)
            if not dry_run:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(content, encoding="utf-8")
                written.append(filename)
            else:
                result_entry["content"] = content

        results[target] = result_entry

    # Generate LEARNINGS.md alongside adapters (advisory)
    learnings_written = False
    if not dry_run:
        try:
            from exo.stdlib.reflect import write_learnings

            write_learnings(repo)
            written.append(".exo/LEARNINGS.md")
            learnings_written = True
        except Exception:  # noqa: BLE001
            pass

    return {
        "targets": chosen,
        "written": written,
        "dry_run": dry_run,
        "generated_at": now_iso(),
        "governance_hash": governance_hash,
        "files": results,
        "learnings_written": learnings_written,
    }
