# Exo Kernel Evolution Policy

This policy defines how `/exo/kernel` may change.

## 1. Kernel Scope

Kernel is only:
- governance enforcement
- side-effect gating
- ticket/lock temporal authority
- immutable audit and receipts

Anything else belongs outside kernel.

## 2. Authority Boundary

Kernel changes are human-only and out-of-band.

Forbidden through Exo governed flows:
- proposal-driven kernel edits
- ticket-based kernel edits
- autonomous kernel patching by agents

## 3. Versioning

Kernel version is semantic: `exo-kernel MAJOR.MINOR.PATCH`.

Kernel version must be embedded in:
- governance lock metadata
- every audit event
- every receipt

## 4. Backward Meaning Invariant

Past decisions and receipts never change meaning.
Old records remain interpreted under the kernel version that created them.

## 5. Change Classes

- PATCH: refactor/perf/logging only, same semantics.
- MINOR: additive, opt-in behavior only.
- MAJOR: semantic changes; requires explicit migration and frozen legacy receipts.

## 6. Prohibition List

Kernel must never include:
- planning
- memory retrieval
- LLM calls
- goal selection
- self-evolution logic

## 7. Operational Rule

If a problem is solvable in governance or stdlib, do not change kernel.
