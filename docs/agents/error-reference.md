# Error Reference

**One-line**: All ExoError codes, their causes, and resolution steps for AI agents mid-session.

## Error Response Shape

All errors follow the same JSON structure:

```json
{
  "ok": false,
  "error": {
    "code": "ERROR_CODE_HERE",
    "message": "Human-readable error message",
    "details": {},
    "blocked": true
  }
}
```

- `blocked: true` — Cannot proceed; fix required before continuing.
- `blocked: false` — Informational; advisory only.

---

## Session Errors

### SESSION_ALREADY_ACTIVE
**Cause**: Actor already has an active session for a different ticket.
**Resolution**: Finish or suspend the existing session first with `exo session-finish` or `exo session-suspend --reason "..."`.
**Details**: `active_session` — contains session metadata for the conflicting session.

### SESSION_BREAK_GLASS_REQUIRED
**Cause**: `--skip-check` passed without `--break-glass-reason`.
**Resolution**: Provide `--break-glass-reason "why bypassing checks"` when using `--skip-check`.

### SESSION_CONTEXT_WINDOW_INVALID
**Cause**: `context_window_tokens` parameter is not a positive integer.
**Resolution**: Pass a valid positive integer for `context_window_tokens`, or omit it.

### SESSION_LOCK_OWNER_MISMATCH
**Cause**: Active lock is owned by a different actor.
**Resolution**: Release the lock with the correct owner first, or use `--acquire-lock` to claim it.
**Details**: `ticket_id`, `lock_owner`, `actor` — shows who owns the lock vs. who is trying to start the session.

### SESSION_MODE_INVALID
**Cause**: Invalid session mode (must be 'work' or 'audit').
**Resolution**: Pass `mode='work'` or `mode='audit'` to `session.start()`.

### SESSION_NOT_ACTIVE
**Cause**: No active session exists for the actor.
**Resolution**: Start a session with `exo session-start --ticket-id <TID>` before calling `session-finish` or `session-suspend`.

### SESSION_NOT_SUSPENDED
**Cause**: No suspended session exists for the actor.
**Resolution**: Cannot resume; start a new session instead with `exo session-start`.

### SESSION_RELEASE_FLAGS_CONFLICT
**Cause**: Both `--release-lock` and `--no-release-lock` flags provided.
**Resolution**: Remove one of the conflicting flags.

### SESSION_STATUS_INVALID
**Cause**: `--set-status` is not one of `keep|review|done`.
**Resolution**: Use `--set-status keep|review|done` at session-finish.

### SESSION_SUMMARY_REQUIRED
**Cause**: Missing `--summary` on session-finish.
**Resolution**: Provide `--summary "what changed"` when finishing a session.

### SESSION_SUSPEND_CORRUPT
**Cause**: Suspended session JSON file is malformed.
**Resolution**: Delete `.exo/memory/suspended/<actor>.suspended.json` and start a fresh session.

### SESSION_SUSPEND_REASON_REQUIRED
**Cause**: Missing `--reason` on session-suspend.
**Resolution**: Provide `--reason "why pausing"` when suspending.

### SESSION_TICKET_MISMATCH
**Cause**: Active session ticket ID does not match requested ticket ID.
**Resolution**: Finish the active session first, or omit `--ticket-id` to use the active session's ticket.

### SESSION_TICKET_REQUIRED
**Cause**: No ticket ID provided and no active lock exists.
**Resolution**: Pass `--ticket-id <TID>` or acquire a lock first with `exo next`.

### SESSION_VERIFY_FAILED
**Cause**: Checks failed at session-finish.
**Resolution**: Fix the failing checks shown in `check_results`, then retry session-finish. Or use `--skip-check --break-glass-reason "..."` to bypass.
**Details**: `check_results` — shows which checks failed.

---

## Governance Errors

### CONSTITUTION_MISSING
**Cause**: `.exo/CONSTITUTION.md` file does not exist.
**Resolution**: Run `exo init` to create the governance scaffold, or restore the constitution file.

### GOVERNANCE_DRIFT
**Cause**: Constitution source hash does not match governance lock hash.
**Resolution**: Run `exo build-governance` to recompile governance from the updated constitution.

### GOVERNANCE_INVALID
**Cause**: Governance lock file is missing required fields (rules list, source hash, kernel metadata).
**Resolution**: Run `exo build-governance` to regenerate the lock file.

### GOVERNANCE_KERNEL_METADATA_MISSING
**Cause**: Governance lock missing kernel version metadata.
**Resolution**: Run `exo build-governance` to regenerate the lock with current kernel metadata.

### GOVERNANCE_LOCK_MISSING
**Cause**: `.exo/governance.lock.json` file does not exist.
**Resolution**: Run `exo build-governance` to compile the constitution into a governance lock.

### RULE_PARSE_ERROR
**Cause**: Constitution policy block missing required fields (`id`, `type`).
**Resolution**: Fix the constitution YAML blocks to include valid `id:` and `type:` fields.

---

## Ticket Errors

### INVALID_PARENT
**Cause**: Ticket hierarchy violation (task parent must be intent/epic; epic parent must be intent).
**Resolution**: Create an intent first with `exo intent-create`, or use a valid parent.
**Details**: `requested_kind`, `parent_id`, `parent_kind`, `allowed_parent_kinds`, `hint`.

### TICKET_NOT_FOUND
**Cause**: Ticket ID does not exist in `.exo/tickets/`.
**Resolution**: Use `exo status` to list valid ticket IDs, or create a new ticket with `exo plan` or `exo ticket-create`.

---

## Lock & Lease Errors

### LOCK_EXPIRED
**Cause**: Ticket lock has expired (duration_hours exceeded).
**Resolution**: Renew the lock with `exo lease-renew --ticket-id <TID>` or acquire a new lock with `exo next`.

### LOCK_NOT_FOUND
**Cause**: No lock file exists.
**Resolution**: Acquire a lock with `exo next` before working on a ticket.

### LOCK_OWNER_MISMATCH
**Cause**: Lock is owned by a different actor.
**Resolution**: Release the lock as the correct owner, or wait for it to expire.

---

## Scaffold & Init Errors

### SCAFFOLD_EXISTS
**Cause**: `.exo/` directory already exists.
**Resolution**: If you want to reinitialize, delete `.exo/` first, or use `exo upgrade` to update an existing scaffold.

---

## General Errors

### CMD_UNKNOWN
**Cause**: Unknown CLI command.
**Resolution**: Check `exo --help` for valid commands.

### UNHANDLED_EXCEPTION
**Cause**: Unexpected error not caught by ExoError.
**Resolution**: Check the error message for stack trace details. Report as a bug if governance-related.
**Details**: `message` — contains exception string.

---

## Quick Diagnostic Flowchart

**If session-finish fails**:
1. Check `SESSION_VERIFY_FAILED` → fix failing checks or use `--skip-check`
2. Check `SESSION_NOT_ACTIVE` → start a session first
3. Check `SESSION_SUMMARY_REQUIRED` → add `--summary "..."`

**If session-start fails**:
1. Check `SESSION_ALREADY_ACTIVE` → finish/suspend existing session
2. Check `SESSION_TICKET_REQUIRED` → pass `--ticket-id` or run `exo next`
3. Check `GOVERNANCE_DRIFT` → run `exo build-governance`

**If governance errors**:
1. Check `CONSTITUTION_MISSING` → run `exo init`
2. Check `GOVERNANCE_DRIFT` → run `exo build-governance`
3. Check `GOVERNANCE_LOCK_MISSING` → run `exo build-governance`

**If ticket errors**:
1. Check `TICKET_NOT_FOUND` → use `exo status` to find valid tickets
2. Check `INVALID_PARENT` → create an intent first with `exo intent-create`
