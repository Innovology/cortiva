# Tamper-Evident Audit Log

Cortiva includes a hash-chained audit log that records events with SHA-256 integrity verification. Every entry contains a hash of its own contents plus the hash of the previous entry, forming a chain that makes any modification to historical entries detectable.

## How It Works

Each audit entry contains:

| Field | Description |
|---|---|
| `sequence_number` | Monotonically increasing index within the day's log file |
| `timestamp` | ISO 8601 UTC timestamp |
| `event_type` | Dot-namespaced event identifier (e.g. `agent.wake`, `task.complete`) |
| `agent_id` | The agent that triggered the event |
| `data` | Arbitrary JSON payload |
| `previous_hash` | SHA-256 hash of the previous entry (or genesis hash for the first) |
| `entry_hash` | SHA-256 hash of this entry's contents combined with `previous_hash` |

### Hash Computation

The hash for each entry is computed from a deterministic string:

```
{sequence_number}:{timestamp}:{event_type}:{agent_id}:{json_data}:{previous_hash}
```

where `json_data` is the `data` dict serialized with sorted keys and compact separators. This string is then hashed with SHA-256.

### Genesis Hash

The very first entry in the log (when no previous day's log exists) chains from a genesis hash: 64 zeros (`0000...0000`).

## Daily File Rotation

Log files are stored as JSONL (one JSON object per line) with daily rotation:

```
audit/
  audit-2026-03-27.jsonl
  audit-2026-03-28.jsonl
  audit-2026-03-29.jsonl
```

Each file is named `audit-YYYY-MM-DD.jsonl`. The first entry of a new day chains from the last entry of the most recent previous day's file, maintaining the hash chain across days.

## Setup

Create an `AuditLog` pointing at your log directory:

```python
from cortiva.core.audit import AuditLog

audit = AuditLog(log_dir="./data/audit")
```

The directory is created automatically (including nested parents) if it does not exist.

## Appending Events

Use `append` to record an event:

```python
entry = audit.append(
    event_type="agent.wake",
    agent_id="dev-cortiva",
    data={"reason": "scheduled"},
)

print(entry.sequence_number)  # 0 (first entry of the day)
print(entry.entry_hash)       # SHA-256 hex string
```

The `data` argument is optional and defaults to an empty dict. The hash chain is maintained automatically -- each new entry's `previous_hash` is set to the `entry_hash` of the preceding entry.

```python
e1 = audit.append("task.start", "dev-cortiva", {"task": "fix-bug-123"})
e2 = audit.append("task.complete", "dev-cortiva", {"task": "fix-bug-123"})

assert e2.previous_hash == e1.entry_hash  # chain is linked
```

## Reading Entries

Read all entries for a specific date:

```python
from datetime import date

entries = audit.read(date.today())
for entry in entries:
    print(f"[{entry.sequence_number}] {entry.event_type} by {entry.agent_id}")
```

Use the `limit` parameter to read only the first N entries:

```python
entries = audit.read(date(2026, 3, 29), limit=10)
```

If no log file exists for the given date, an empty list is returned.

## Verifying Integrity

The `verify` method checks the hash chain for a given date. It recomputes every hash and confirms that each entry's `previous_hash` matches the preceding entry's `entry_hash`:

```python
valid, broken_at = audit.verify(date.today())

if valid:
    print("Log integrity verified")
else:
    print(f"Chain broken at sequence number {broken_at}")
```

`verify` returns a tuple of `(bool, int | None)`:

- `(True, None)` -- the chain is intact
- `(False, sequence_number)` -- the first entry where the chain breaks

Verification catches:

- Modified event data (changed payloads)
- Corrupted hashes
- Deleted or reordered entries
- Any modification that breaks the hash chain

If no entries exist for the given date, verification returns `(True, None)`.

## Cross-Day Chaining

The first entry of each day chains from the last entry of the most recent previous day's log file. This means tampering with any past day's entries will also break verification for the following day.

If no previous day's log exists (e.g. the very first day of logging, or after a gap), the entry chains from the genesis hash.

## What Gets Logged

The audit log is a general-purpose event store. You decide what to log by calling `append` with appropriate event types. Common patterns in Cortiva deployments:

- `agent.wake` / `agent.sleep` -- lifecycle events
- `task.start` / `task.complete` / `task.escalate` -- task tracking
- `governance.approval` / `governance.deny` -- authority decisions
- `config.change` -- configuration modifications
- `budget.spend` -- consciousness budget usage

The `data` payload is freeform JSON, so you can include whatever context is relevant to the event.
