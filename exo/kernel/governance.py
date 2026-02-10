from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .errors import ExoError
from .types import Governance, VerificationReport
from .utils import any_pattern_matches, dump_json, load_json, now_iso, parse_yaml_like, sha256_text
from .version import KERNEL_NAME, KERNEL_VERSION, is_supported_kernel_version


CONSTITUTION_PATH = Path(".exo/CONSTITUTION.md")
LOCK_PATH = Path(".exo/governance.lock.json")


POLICY_BLOCK_RE = re.compile(r"```yaml\s+exo-policy\s*\n(.*?)```", re.DOTALL)


def extract_policy_blocks(text: str) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for raw in POLICY_BLOCK_RE.findall(text):
        parsed = parse_yaml_like(raw)
        if not isinstance(parsed, dict):
            continue
        rules.append(parsed)
    return rules


def compile_constitution(repo: Path, version: str = "0.1") -> dict[str, Any]:
    constitution_path = repo / CONSTITUTION_PATH
    if not constitution_path.exists():
        raise ExoError(
            code="CONSTITUTION_MISSING",
            message="Missing .exo/CONSTITUTION.md",
        )

    source_text = constitution_path.read_text(encoding="utf-8")
    rules = extract_policy_blocks(source_text)

    bad_rules = [rule for rule in rules if "id" not in rule or "type" not in rule]
    if bad_rules:
        raise ExoError(
            code="RULE_PARSE_ERROR",
            message="Constitution policy block missing required fields (id/type)",
            details={"bad_rule_count": len(bad_rules)},
        )

    lock_data: dict[str, Any] = {
        "version": version,
        "generated_at": now_iso(),
        "source_file": str(CONSTITUTION_PATH),
        "source_hash": sha256_text(source_text),
        "kernel": {
            "name": KERNEL_NAME,
            "version": KERNEL_VERSION,
        },
        "rules": rules,
    }
    dump_json(repo / LOCK_PATH, lock_data)
    return lock_data


def load_governance_lock(repo: Path) -> dict[str, Any]:
    lock_path = repo / LOCK_PATH
    if not lock_path.exists():
        raise ExoError(
            code="GOVERNANCE_LOCK_MISSING",
            message="Missing .exo/governance.lock.json. Run: exo build-governance",
        )
    return load_json(lock_path)


def load_governance(root: Path | str) -> Governance:
    repo = Path(root).resolve()
    lock_data = load_governance_lock(repo)

    rules = lock_data.get("rules")
    if not isinstance(rules, list):
        raise ExoError(
            code="GOVERNANCE_INVALID",
            message="Governance lock missing rules list",
            details={"field": "rules"},
        )

    source_hash = str(lock_data.get("source_hash", "")).strip()
    if not source_hash:
        raise ExoError(
            code="GOVERNANCE_INVALID",
            message="Governance lock missing source_hash",
            details={"field": "source_hash"},
        )

    constitution_path = repo / CONSTITUTION_PATH
    actual_source_hash = ""
    if constitution_path.exists():
        actual_source_hash = sha256_text(constitution_path.read_text(encoding="utf-8"))

    source_file = str(lock_data.get("source_file") or str(CONSTITUTION_PATH))
    return Governance(
        root=repo.as_posix(),
        source_file=source_file,
        source_hash=source_hash,
        actual_source_hash=actual_source_hash,
        rules=rules,
        lock_data=lock_data,
    )


def verify_governance(gov: Governance) -> VerificationReport:
    reasons: list[str] = []

    if not gov.source_hash:
        reasons.append("Missing governance source_hash")

    if gov.source_hash != gov.actual_source_hash:
        reasons.append("Governance source hash mismatch")

    for issue in sanity_check_rules(gov.lock_data):
        reasons.append(f"Rule sanity: {issue}")

    kernel_meta = gov.lock_data.get("kernel")
    if not isinstance(kernel_meta, dict):
        reasons.append("Governance lock missing kernel metadata")
    else:
        locked_kernel_name = str(kernel_meta.get("name", "")).strip()
        locked_kernel_version = str(kernel_meta.get("version", "")).strip()
        if locked_kernel_name != KERNEL_NAME:
            reasons.append(f"Kernel name mismatch: expected {KERNEL_NAME}, got {locked_kernel_name or 'missing'}")
        if not locked_kernel_version:
            reasons.append("Governance lock missing kernel.version")
        elif not is_supported_kernel_version(locked_kernel_version):
            reasons.append(
                f"Unsupported governance kernel.version {locked_kernel_version} for running kernel {KERNEL_VERSION}"
            )

    return VerificationReport(
        valid=(len(reasons) == 0),
        reasons=reasons,
        expected_hash=gov.source_hash or None,
        actual_hash=gov.actual_source_hash or None,
    )


def verify_integrity(repo: Path) -> dict[str, Any]:
    lock_data = load_governance_lock(repo)
    constitution_path = repo / CONSTITUTION_PATH
    if not constitution_path.exists():
        raise ExoError(
            code="CONSTITUTION_MISSING",
            message="Missing .exo/CONSTITUTION.md",
        )

    source_text = constitution_path.read_text(encoding="utf-8")
    expected_hash = lock_data.get("source_hash")
    actual_hash = sha256_text(source_text)

    if expected_hash != actual_hash:
        raise ExoError(
            code="GOVERNANCE_DRIFT",
            message="Constitution drift detected. Run: exo build-governance",
            details={"expected_hash": expected_hash, "actual_hash": actual_hash},
            blocked=True,
        )

    kernel_meta = lock_data.get("kernel")
    if not isinstance(kernel_meta, dict):
        raise ExoError(
            code="GOVERNANCE_KERNEL_METADATA_MISSING",
            message="Governance lock missing kernel metadata. Run: exo build-governance",
            blocked=True,
        )
    lock_kernel_name = str(kernel_meta.get("name", "")).strip()
    lock_kernel_version = str(kernel_meta.get("version", "")).strip()
    if lock_kernel_name != KERNEL_NAME:
        raise ExoError(
            code="KERNEL_NAME_MISMATCH",
            message=f"Governance lock kernel name mismatch: expected {KERNEL_NAME}, got {lock_kernel_name or 'missing'}",
            details={"expected": KERNEL_NAME, "actual": lock_kernel_name or None},
            blocked=True,
        )
    if not lock_kernel_version:
        raise ExoError(
            code="GOVERNANCE_KERNEL_VERSION_MISSING",
            message="Governance lock missing kernel.version. Run: exo build-governance",
            blocked=True,
        )
    if not is_supported_kernel_version(lock_kernel_version):
        raise ExoError(
            code="KERNEL_VERSION_UNSUPPORTED",
            message=(
                f"Governance lock kernel.version {lock_kernel_version} is unsupported by runtime "
                f"{KERNEL_VERSION}. Rebuild governance or run compatible kernel."
            ),
            details={"lock_kernel_version": lock_kernel_version, "runtime_kernel_version": KERNEL_VERSION},
            blocked=True,
        )
    return lock_data


def sanity_check_rules(lock_data: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    rules = lock_data.get("rules", [])
    if not isinstance(rules, list):
        issues.append("rules must be a list")
        return issues

    seen: set[str] = set()
    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            issues.append(f"rule[{idx}] is not an object")
            continue
        rid = rule.get("id")
        rtype = rule.get("type")
        if not rid:
            issues.append(f"rule[{idx}] missing id")
            continue
        if rid in seen:
            issues.append(f"duplicate rule id: {rid}")
        seen.add(rid)
        if not rtype:
            issues.append(f"rule {rid} missing type")

    return issues


def _rule_applies(rule: dict[str, Any], action: str, path: Path, repo: Path) -> bool:
    actions = rule.get("actions")
    if actions and action not in actions:
        return False

    patterns = rule.get("patterns", [])
    if not patterns:
        return True
    if not isinstance(patterns, list):
        return False
    return any_pattern_matches(path, patterns, repo)


def evaluate_filesystem_rules(
    lock_data: dict[str, Any],
    action: str,
    path: Path,
    repo: Path,
) -> dict[str, Any] | None:
    rules = lock_data.get("rules", [])
    allow_match: dict[str, Any] | None = None
    deny_match: dict[str, Any] | None = None

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("type") != "filesystem_allow":
            continue
        if _rule_applies(rule, action, path, repo):
            allow_match = rule
            break

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("type") != "filesystem_deny":
            continue
        if _rule_applies(rule, action, path, repo):
            deny_match = rule
            break

    if deny_match and allow_match:
        return None
    return deny_match


def has_rule(lock_data: dict[str, Any], rule_type: str) -> bool:
    rules = lock_data.get("rules", [])
    return any(isinstance(rule, dict) and rule.get("type") == rule_type for rule in rules)
