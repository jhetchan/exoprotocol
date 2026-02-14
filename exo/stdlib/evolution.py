from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from exo.kernel.utils import dump_yaml, ensure_dir, load_yaml, now_iso


OBS_DIR = Path(".exo/observations")
PATCH_DIR = Path(".exo/patches")
PROP_DIR = Path(".exo/proposals")
REV_DIR = Path(".exo/reviews")
PRACTICES_DIR = Path(".exo/practices")
ROLES_DIR = Path(".exo/roles")
MEMORY_DIR = Path(".exo/memory")
TEMPLATES_DIR = Path(".exo/templates")
SCHEMAS_DIR = Path(".exo/schemas")

MEMORY_INDEX_PATH = MEMORY_DIR / "index.yaml"
PROPOSAL_SCHEMA_PATH = SCHEMAS_DIR / "proposal.schema.json"

OBS_TEMPLATE_PATH = TEMPLATES_DIR / "OBS.template.md"
PROP_TEMPLATE_PATH = TEMPLATES_DIR / "PROP.template.yaml"
REV_TEMPLATE_PATH = TEMPLATES_DIR / "REV.template.md"
MEMORY_TEMPLATE_PATH = TEMPLATES_DIR / "memory.index.template.yaml"

AUDIT_LOG_PATH = Path(".exo/logs/audit.log.jsonl")


KIND_VALUES = {"practice_change", "governance_change", "tooling_change"}
STATUS_VALUES = {"draft", "proposed", "approved", "rejected", "applied", "rolled_back"}
RISK_VALUES = {"low", "medium", "high"}
CONFIDENCE_VALUES = {"low", "medium", "high"}


OBS_TEMPLATE = """---
id: OBS-YYYY-MM-DD-001
timestamp: YYYY-MM-DDTHH:MM:SS+00:00
ticket: TICKET-NNN
actor: agent:your-agent
trigger:
  - blocked_by_governance
  - test_failure
tags:
  - drift
  - scope
confidence: high
---

## What happened
Describe what the agent attempted and what happened.

## Evidence
- audit log lines: NNN-NNN
- command: exo do TICKET-NNN

## Immediate outcome
What was the result (blocked, failed, etc.)?

## Notes
Pure observation. No interpretation or fix proposed here.
"""


PROP_TEMPLATE = """id: PROP-NNN
created_at: YYYY-MM-DDTHH:MM:SS+00:00
author: human:your-name
ticket: TICKET-NNN
kind: practice_change
status: draft
summary: >
  Describe the proposed change.
symptom:
  - "What behavior triggered this proposal"
root_cause: >
  Why the current rules don't cover this case.
proposed_change:
  type: add_file
  path: .exo/practices/your-practice.md
expected_effect:
  - "What should improve"
risk_level: low
blast_radius:
  - practices_only
rollback:
  type: delete_file
  path: .exo/practices/your-practice.md
evidence:
  observations:
    - OBS-YYYY-MM-DD-001
  audit_log_ranges:
    - "audit.log.jsonl:NNN-NNN"
requires:
  approvals: 1
  human_required: false
notes:
  - "No governance change"
"""


REV_TEMPLATE = """---
review_id: REV-NNN-001
proposal: PROP-NNN
reviewer: human:your-name
timestamp: YYYY-MM-DDTHH:MM:SS+00:00
decision: approve
confidence: high
---

## Rationale
Explain why you approve or reject the proposal.

## Conditions
None.

## Signature
your-name
"""


MEMORY_INDEX_TEMPLATE = {
    "version": "0.1",
    "last_updated": datetime.now().astimezone().date().isoformat(),
    "rules_of_thumb": [],
    "failure_modes": [],
}

MEMORY_TEMPLATE_TEXT = """version: 0.1
last_updated: 2026-02-10
rules_of_thumb: []
failure_modes: []
"""


def proposal_schema_template() -> dict[str, Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Exo Proposal",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "id",
            "created_at",
            "author",
            "kind",
            "status",
            "summary",
            "symptom",
            "root_cause",
            "proposed_change",
            "risk_level",
            "rollback",
        ],
        "properties": {
            "id": {"type": "string", "pattern": "^PROP-[0-9]{3,}$"},
            "created_at": {"type": "string", "format": "date-time"},
            "author": {"type": "string", "minLength": 1},
            "ticket": {"type": "string"},
            "kind": {
                "type": "string",
                "enum": ["practice_change", "governance_change", "tooling_change"],
            },
            "status": {
                "type": "string",
                "enum": ["draft", "proposed", "approved", "rejected", "applied", "rolled_back"],
            },
            "summary": {"type": "string", "minLength": 10},
            "symptom": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
            },
            "root_cause": {"type": "string", "minLength": 10},
            "proposed_change": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "path"],
                "properties": {
                    "type": {"type": "string", "minLength": 1},
                    "path": {"type": "string", "minLength": 1},
                },
            },
            "expected_effect": {
                "type": "array",
                "items": {"type": "string"},
            },
            "risk_level": {
                "type": "string",
                "enum": ["low", "medium", "high"],
            },
            "blast_radius": {
                "type": "array",
                "items": {"type": "string"},
            },
            "rollback": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "path"],
                "properties": {
                    "type": {"type": "string", "minLength": 1},
                    "path": {"type": "string", "minLength": 1},
                },
            },
            "requires": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "approvals": {"type": "integer", "minimum": 1},
                    "human_required": {"type": "boolean"},
                },
            },
            "evidence": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "observations": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "audit_log_ranges": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
            "notes": {
                "type": "array",
                "items": {"type": "string"},
            },
            "approvals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["review_id", "decision", "reviewer", "reviewer_type", "created_at"],
                    "properties": {
                        "review_id": {"type": "string"},
                        "decision": {"type": "string", "enum": ["approved", "rejected"]},
                        "reviewer": {"type": "string"},
                        "reviewer_type": {"type": "string", "enum": ["human", "agent"]},
                        "note": {"type": "string"},
                        "created_at": {"type": "string", "format": "date-time"},
                    },
                },
            },
            "updated_at": {"type": "string", "format": "date-time"},
            "applied_at": {"type": "string", "format": "date-time"},
            "applied_by": {"type": "string"},
            "applied_files": {
                "type": "array",
                "items": {"type": "string"},
            },
            "distilled_at": {"type": "string", "format": "date-time"},
            "distilled_by": {"type": "string"},
        },
    }


def default_memory_index() -> dict[str, Any]:
    return {
        "version": "0.1",
        "last_updated": datetime.now().astimezone().date().isoformat(),
        "rules_of_thumb": [],
        "failure_modes": [],
    }


def ensure_layout(repo: Path) -> None:
    dirs = [
        OBS_DIR,
        PATCH_DIR,
        PROP_DIR,
        REV_DIR,
        PRACTICES_DIR,
        ROLES_DIR,
        MEMORY_DIR,
        TEMPLATES_DIR,
        SCHEMAS_DIR,
    ]
    for directory in dirs:
        ensure_dir(repo / directory)

    memory_index = repo / MEMORY_INDEX_PATH
    if not memory_index.exists():
        dump_yaml(memory_index, default_memory_index())

    schema_path = repo / PROPOSAL_SCHEMA_PATH
    if not schema_path.exists():
        schema_path.write_text(json.dumps(proposal_schema_template(), indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    templates: list[tuple[Path, str]] = [
        (repo / OBS_TEMPLATE_PATH, OBS_TEMPLATE),
        (repo / PROP_TEMPLATE_PATH, PROP_TEMPLATE),
        (repo / REV_TEMPLATE_PATH, REV_TEMPLATE),
    ]
    for path, content in templates:
        if not path.exists():
            path.write_text(content, encoding="utf-8")

    memory_template_path = repo / MEMORY_TEMPLATE_PATH
    if not memory_template_path.exists():
        memory_template_path.write_text(MEMORY_TEMPLATE_TEXT, encoding="utf-8")


def _next_simple_id(repo: Path, directory: Path, prefix: str, suffix: str, width: int = 3) -> str:
    ensure_dir(repo / directory)
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+){re.escape(suffix)}$")

    values: list[int] = []
    for path in (repo / directory).glob(f"{prefix}-*{suffix}"):
        match = pattern.match(path.name)
        if match:
            values.append(int(match.group(1)))

    nxt = max(values) + 1 if values else 1
    return f"{prefix}-{nxt:0{width}d}"


def next_observation_id(repo: Path) -> str:
    today = datetime.now().astimezone().date().isoformat()
    ensure_dir(repo / OBS_DIR)
    pattern = re.compile(rf"^OBS-{re.escape(today)}-(\d{{3}})\.md$")

    values: list[int] = []
    for path in (repo / OBS_DIR).glob(f"OBS-{today}-*.md"):
        match = pattern.match(path.name)
        if match:
            values.append(int(match.group(1)))

    nxt = max(values) + 1 if values else 1
    return f"OBS-{today}-{nxt:03d}"


def next_patch_id(repo: Path) -> str:
    return _next_simple_id(repo, PATCH_DIR, "PATCH", ".json")


def next_proposal_id(repo: Path) -> str:
    return _next_simple_id(repo, PROP_DIR, "PROP", ".yaml")


def next_review_id(repo: Path, proposal_id: str) -> str:
    pid = normalize_id(proposal_id, "PROP")
    number = "000"
    match = re.match(r"^PROP-(\d{3,})$", pid)
    if match:
        number = match.group(1)

    ensure_dir(repo / REV_DIR)
    pattern = re.compile(rf"^REV-{re.escape(number)}-(\d{{3}})\.md$")
    values: list[int] = []
    for path in (repo / REV_DIR).glob(f"REV-{number}-*.md"):
        match = pattern.match(path.name)
        if match:
            values.append(int(match.group(1)))
    nxt = max(values) + 1 if values else 1
    return f"REV-{number}-{nxt:03d}"


def observation_path(repo: Path, observation_id: str) -> Path:
    return repo / OBS_DIR / f"{observation_id}.md"


def patch_path(repo: Path, patch_id: str) -> Path:
    return repo / PATCH_DIR / f"{patch_id}.json"


def proposal_path(repo: Path, proposal_id: str) -> Path:
    return repo / PROP_DIR / f"{proposal_id}.yaml"


def review_path(repo: Path, review_id: str) -> Path:
    return repo / REV_DIR / f"{review_id}.md"


def normalize_id(raw: str, prefix: str) -> str:
    value = raw.strip()
    up = value.upper()
    if up.startswith(f"{prefix}-"):
        return up
    if value.isdigit():
        return f"{prefix}-{int(value):03d}"
    return value


def patch_id_for_proposal(proposal_id: str) -> str:
    pid = normalize_id(proposal_id, "PROP")
    match = re.match(r"^PROP-(\d{3,})$", pid)
    if match:
        return f"PATCH-{match.group(1)}"
    return "PATCH-000"


def default_patch_ref_for_proposal(proposal_id: str) -> str:
    patch_id = patch_id_for_proposal(proposal_id)
    return str((PATCH_DIR / f"{patch_id}.json").as_posix())


def load_proposal(repo: Path, proposal_id: str) -> tuple[dict[str, Any], Path]:
    pid = normalize_id(proposal_id, "PROP")
    path = proposal_path(repo, pid)
    if not path.exists():
        raise FileNotFoundError(pid)

    proposal = load_yaml(path)
    if not isinstance(proposal, dict):
        proposal = {}
    proposal.setdefault("id", pid)
    return proposal, path


def save_proposal(repo: Path, proposal: dict[str, Any]) -> Path:
    proposal_id = str(proposal.get("id", "")).strip()
    if not proposal_id:
        raise ValueError("proposal missing id")
    path = proposal_path(repo, proposal_id)
    dump_yaml(path, proposal)
    return path


def load_proposal_schema(repo: Path) -> dict[str, Any]:
    path = repo / PROPOSAL_SCHEMA_PATH
    if not path.exists():
        return proposal_schema_template()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return proposal_schema_template()
    if not isinstance(data, dict):
        return proposal_schema_template()
    return data


def _is_iso_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _validate_string_list(value: Any, field: str, min_items: int = 0) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, list):
        return [f"{field} must be an array of strings"]
    if len(value) < min_items:
        errors.append(f"{field} must include at least {min_items} item(s)")
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"{field}[{idx}] must be a string")
        elif not item.strip():
            errors.append(f"{field}[{idx}] must not be empty")
    return errors


def validate_proposal_payload(proposal: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    if not isinstance(proposal, dict):
        return ["proposal must be an object"]

    schema = proposal_schema_template()
    props = schema.get("properties", {})
    required = schema.get("required", [])

    for key in required:
        if key not in proposal:
            errors.append(f"missing required field: {key}")

    allowed_fields = set(props.keys())
    for key in proposal.keys():
        if key not in allowed_fields:
            errors.append(f"unknown field: {key}")

    pid = proposal.get("id")
    if not isinstance(pid, str) or not re.match(r"^PROP-[0-9]{3,}$", pid):
        errors.append("id must match ^PROP-[0-9]{3,}$")

    created_at = proposal.get("created_at")
    if not isinstance(created_at, str) or not _is_iso_datetime(created_at):
        errors.append("created_at must be RFC3339 date-time")

    author = proposal.get("author")
    if not isinstance(author, str) or not author.strip():
        errors.append("author must be a non-empty string")

    kind = proposal.get("kind")
    if kind not in KIND_VALUES:
        errors.append("kind must be one of practice_change|governance_change|tooling_change")

    status = proposal.get("status")
    if status not in STATUS_VALUES:
        errors.append("status must be one of draft|proposed|approved|rejected|applied|rolled_back")

    summary = proposal.get("summary")
    if not isinstance(summary, str) or len(summary.strip()) < 10:
        errors.append("summary must be a string with min length 10")

    symptom = proposal.get("symptom")
    errors.extend(_validate_string_list(symptom, "symptom", min_items=1))

    root_cause = proposal.get("root_cause")
    if not isinstance(root_cause, str) or len(root_cause.strip()) < 10:
        errors.append("root_cause must be a string with min length 10")

    proposed_change = proposal.get("proposed_change")
    if not isinstance(proposed_change, dict):
        errors.append("proposed_change must be an object")
    else:
        for key in ["type", "path"]:
            value = proposed_change.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"proposed_change.{key} must be a non-empty string")
        for key in proposed_change.keys():
            if key not in {"type", "path"}:
                errors.append(f"unknown field: proposed_change.{key}")

    expected_effect = proposal.get("expected_effect")
    if expected_effect is not None:
        errors.extend(_validate_string_list(expected_effect, "expected_effect"))

    blast_radius = proposal.get("blast_radius")
    if blast_radius is not None:
        errors.extend(_validate_string_list(blast_radius, "blast_radius"))

    risk_level = proposal.get("risk_level")
    if risk_level not in RISK_VALUES:
        errors.append("risk_level must be one of low|medium|high")

    rollback = proposal.get("rollback")
    if not isinstance(rollback, dict):
        errors.append("rollback must be an object")
    else:
        for key in ["type", "path"]:
            value = rollback.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"rollback.{key} must be a non-empty string")
        for key in rollback.keys():
            if key not in {"type", "path"}:
                errors.append(f"unknown field: rollback.{key}")

    requires = proposal.get("requires")
    if requires is not None:
        if not isinstance(requires, dict):
            errors.append("requires must be an object")
        else:
            approvals = requires.get("approvals")
            if approvals is not None and (not isinstance(approvals, int) or approvals < 1):
                errors.append("requires.approvals must be an integer >= 1")
            human_required = requires.get("human_required")
            if human_required is not None and not isinstance(human_required, bool):
                errors.append("requires.human_required must be a boolean")
            for key in requires.keys():
                if key not in {"approvals", "human_required"}:
                    errors.append(f"unknown field: requires.{key}")

    evidence = proposal.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, dict):
            errors.append("evidence must be an object")
        else:
            obs = evidence.get("observations")
            if obs is not None:
                errors.extend(_validate_string_list(obs, "evidence.observations"))
            audit_ranges = evidence.get("audit_log_ranges")
            if audit_ranges is not None:
                errors.extend(_validate_string_list(audit_ranges, "evidence.audit_log_ranges"))
            for key in evidence.keys():
                if key not in {"observations", "audit_log_ranges"}:
                    errors.append(f"unknown field: evidence.{key}")

    notes = proposal.get("notes")
    if notes is not None:
        errors.extend(_validate_string_list(notes, "notes"))

    approvals = proposal.get("approvals")
    if approvals is not None:
        if not isinstance(approvals, list):
            errors.append("approvals must be an array")
        else:
            for idx, review in enumerate(approvals):
                if not isinstance(review, dict):
                    errors.append(f"approvals[{idx}] must be an object")
                    continue
                required_review = {"review_id", "decision", "reviewer", "reviewer_type", "created_at"}
                for key in required_review:
                    if key not in review:
                        errors.append(f"approvals[{idx}] missing required field: {key}")
                for key in review.keys():
                    if key not in {"review_id", "decision", "reviewer", "reviewer_type", "note", "created_at"}:
                        errors.append(f"unknown field: approvals[{idx}].{key}")
                decision = review.get("decision")
                if decision not in {"approved", "rejected"}:
                    errors.append(f"approvals[{idx}].decision must be approved|rejected")
                reviewer_type = review.get("reviewer_type")
                if reviewer_type not in {"human", "agent"}:
                    errors.append(f"approvals[{idx}].reviewer_type must be human|agent")
                created = review.get("created_at")
                if not isinstance(created, str) or not _is_iso_datetime(created):
                    errors.append(f"approvals[{idx}].created_at must be RFC3339 date-time")

    for time_field in ["updated_at", "applied_at", "distilled_at"]:
        value = proposal.get(time_field)
        if value is not None and (not isinstance(value, str) or not _is_iso_datetime(value)):
            errors.append(f"{time_field} must be RFC3339 date-time")

    for list_field in ["applied_files"]:
        value = proposal.get(list_field)
        if value is not None:
            errors.extend(_validate_string_list(value, list_field))

    for str_field in ["ticket", "applied_by", "distilled_by"]:
        value = proposal.get(str_field)
        if value is not None and not isinstance(value, str):
            errors.append(f"{str_field} must be a string")

    if kind == "governance_change":
        req = proposal.get("requires")
        human_required = req.get("human_required") if isinstance(req, dict) else None
        if human_required is not True:
            errors.append("governance_change requires requires.human_required=true")

    return errors


def validate_proposal(repo: Path, proposal: dict[str, Any]) -> list[str]:
    schema = load_proposal_schema(repo)
    manual_errors = validate_proposal_payload(proposal)
    try:
        import jsonschema  # type: ignore
    except ModuleNotFoundError:
        return manual_errors

    try:
        jsonschema.validate(instance=proposal, schema=schema)
    except Exception as exc:  # noqa: BLE001
        manual_errors.append(f"jsonschema: {exc}")
    return manual_errors


def patch_placeholder() -> dict[str, Any]:
    return {
        "patches": [],
        "meta": {
            "created_at": now_iso(),
            "note": "Fill patches with [{path, content}] entries before apply.",
        },
    }


def actor_type(actor: str) -> str:
    lowered = actor.strip().lower()
    return "human" if lowered == "human" or lowered.startswith("human:") else "agent"


def approved_reviews(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    raw = proposal.get("approvals")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and item.get("decision") == "approved"]


def gate_summary(proposal: dict[str, Any], trusted_approvers: list[str]) -> dict[str, Any]:
    approvals = approved_reviews(proposal)
    kind = str(proposal.get("kind", ""))
    trusted = set(str(item) for item in trusted_approvers)

    requires = proposal.get("requires")
    required_approvals = 1
    requires_human = kind == "governance_change"
    if isinstance(requires, dict):
        raw_approvals = requires.get("approvals")
        if isinstance(raw_approvals, int) and raw_approvals >= 1:
            required_approvals = raw_approvals
        raw_human = requires.get("human_required")
        if isinstance(raw_human, bool):
            requires_human = raw_human

    human_approvals = [item for item in approvals if item.get("reviewer_type") == "human"]
    trusted_or_human = [
        item
        for item in approvals
        if item.get("reviewer_type") == "human" or str(item.get("reviewer")) in trusted
    ]

    ready = len(trusted_or_human) >= required_approvals
    if requires_human:
        ready = ready and len(human_approvals) >= 1

    return {
        "kind": kind,
        "requires_human": requires_human,
        "min_approvals": required_approvals,
        "approved_count": len(approvals),
        "human_approved_count": len(human_approvals),
        "trusted_or_human_count": len(trusted_or_human),
        "ready": ready,
    }


def last_governance_apply_ts(repo: Path) -> datetime | None:
    path = repo / AUDIT_LOG_PATH
    if not path.exists():
        return None

    latest: datetime | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if item.get("action") != "apply_proposal":
            continue
        if item.get("result") != "ok":
            continue
        details = item.get("details")
        if not isinstance(details, dict):
            continue
        if details.get("kind") != "governance_change":
            continue
        ts_raw = item.get("ts")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        latest = ts if (latest is None or ts > latest) else latest
    return latest


def load_memory_index(repo: Path) -> dict[str, Any]:
    path = repo / MEMORY_INDEX_PATH
    if not path.exists():
        return default_memory_index()
    data = load_yaml(path)
    if not isinstance(data, dict):
        return default_memory_index()
    data.setdefault("version", "0.1")
    data.setdefault("last_updated", datetime.now().astimezone().date().isoformat())
    data.setdefault("rules_of_thumb", [])
    data.setdefault("failure_modes", [])
    return data


def save_memory_index(repo: Path, index_data: dict[str, Any]) -> Path:
    path = repo / MEMORY_INDEX_PATH
    dump_yaml(path, index_data)
    return path


def next_memory_id(index_data: dict[str, Any], prefix: str) -> str:
    values: list[int] = []
    for section in ["rules_of_thumb", "failure_modes"]:
        raw = index_data.get(section)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            iid = str(item.get("id", ""))
            if not iid.startswith(f"{prefix}-"):
                continue
            number = iid.replace(f"{prefix}-", "")
            if number.isdigit():
                values.append(int(number))
    nxt = max(values) + 1 if values else 1
    return f"{prefix}-{nxt:03d}"
