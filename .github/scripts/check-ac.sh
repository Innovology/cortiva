#!/usr/bin/env bash
# Validates the Acceptance Criteria section and Agent Block in a PR body.
#
# Fails if:
#   - ## Acceptance Criteria section is missing
#   - No checked items (- [x] ...) exist
#   - Any unchecked item is not written as "N/A — <reason>"
#   - Any checked item has no verification note after the "—" separator
#   - Agent Block is present with content but is missing required fields
#
# Env vars required:  PR_BODY

set -euo pipefail

PR_BODY="${PR_BODY:-}"

python3 - <<'PYEOF'
import os, re, sys

body = os.environ.get("PR_BODY", "")

RED = "\033[0;31m"
GRN = "\033[0;32m"
NC  = "\033[0m"

def fail(msg):
    print(f"{RED}[AC LINT] FAIL: {msg}{NC}", file=sys.stderr)
    sys.exit(1)

def ok(msg):
    print(f"{GRN}[AC LINT] PASS: {msg}{NC}")

# ── 1. Extract ## Acceptance Criteria section ─────────────────────────────────
m = re.search(
    r"^##\s+Acceptance\s+Criteria\s*\n([\s\S]*?)(?=\n##\s|\Z)",
    body, re.MULTILINE | re.IGNORECASE
)
if not m:
    fail(
        "No '## Acceptance Criteria' section found. "
        "Add the section and fill in what you verified — see the PR template."
    )

section_raw = m.group(1)
# Strip HTML comments — they are guidance, not content
section = re.sub(r"<!--.*?-->", "", section_raw, flags=re.DOTALL)

# ── 2. Must have at least one checkbox item ───────────────────────────────────
all_items = re.findall(r"^[-*]\s+\[([ xX])\]\s*(.*)", section, re.MULTILINE)
if not all_items:
    fail(
        "Acceptance Criteria section has no checklist items. "
        "Add '- [x] Label — what you verified' entries."
    )

checked   = [(s, t.strip()) for s, t in all_items if s.lower() == "x"]
unchecked = [(s, t.strip()) for s, t in all_items if s == " "]

# ── 3. At least one item must be checked ─────────────────────────────────────
if not checked:
    fail(
        "No acceptance criteria are checked. "
        "Check each criterion you verified, and fill in how you verified it."
    )

# ── 4. Checked items must have a verification note after the separator ────────
SEP = re.compile(r"\s+[-—]\s+")  # matches " - " or " — "
for _, text in checked:
    parts = SEP.split(text, maxsplit=1)
    note = parts[1].strip() if len(parts) > 1 else ""
    if not note:
        label = text[:60].rstrip()
        fail(
            f"Checked item '{label}' has no verification note. "
            "Write what you verified after the '—' separator, e.g. '- [x] Tests — pytest 47/47 pass'."
        )

# ── 5. Unchecked items must be N/A with a reason ─────────────────────────────
for _, text in unchecked:
    if not re.search(r"\bN/A\s*[-—]\s*\S", text, re.IGNORECASE):
        label = text[:60].rstrip()
        fail(
            f"Unchecked item '{label}' must be either checked or marked 'N/A — <reason>'. "
            "Check it if you verified it; otherwise write why it doesn't apply."
        )

# ── 6. Agent Block: if present with content, enforce required fields ──────────
agent_m = re.search(
    r"^##\s+Agent\s+Block\b[^\n]*\n([\s\S]*?)(?=\n##\s|\Z)",
    body, re.MULTILINE
)
if agent_m:
    agent_raw = agent_m.group(1)
    agent_clean = re.sub(r"<!--.*?-->", "", agent_raw, flags=re.DOTALL)
    non_empty = [l.strip() for l in agent_clean.splitlines() if l.strip()]
    if non_empty:
        for field in ("Prompt hash", "Model", "Verification"):
            fm = re.search(
                r"\*\*" + re.escape(field) + r":?\*\*:?\s*(\S+)",
                agent_clean, re.IGNORECASE
            )
            if not fm:
                fail(
                    f"Agent Block is present but '{field}' is missing or empty. "
                    "Fill all three agent block fields, or delete the section for human-authored PRs."
                )

n_checked = len(checked)
n_na      = len(unchecked)
ok(f"{n_checked} criteria verified, {n_na} marked N/A — AC gate cleared.")
PYEOF
