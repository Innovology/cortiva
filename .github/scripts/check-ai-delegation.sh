#!/usr/bin/env bash
# Validates the AI Delegation field in a PR body.
# Exits 1 if the field is missing, is the placeholder, or the Prompt owner is
# absent/malformed.
#
# Supports the single PR template format used across Innovology repos:
#   **AI Delegation:** <description>
#   **Prompt owner:** @<handle>

set -euo pipefail

PR_BODY="${PR_BODY:-}"

RED='[0;31m'
YEL='[1;33m'
GRN='[0;32m'
NC='[0m'

fail()  { echo -e "${RED}[AI-DELEG] FAIL: $*${NC}" >&2; exit 1; }
warn()  { echo -e "${YEL}[AI-DELEG] WARN: $*${NC}"; }
pass()  { echo -e "${GRN}[AI-DELEG] PASS: $*${NC}"; }

# ── 0. Skip automated bots ────────────────────────────────────────────────────
# Dependabot and other bots don't fill in PR fields.
if echo "$PR_BODY" | grep -qE '^Bumps \['; then
  pass "Dependabot PR — AI Delegation check skipped."
  exit 0
fi
if [[ "${GITHUB_ACTOR:-}" =~ \[bot\]$ ]]; then
  pass "Bot actor (${GITHUB_ACTOR}) — AI Delegation check skipped."
  exit 0
fi

# ── 1. Extract **AI Delegation:** value ───────────────────────────────────────
DELEG_VALUE=$(python3 -c '
import os, re, sys

body = os.environ.get("PR_BODY", "")

# Strip HTML comments before extraction
body_clean = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)

m = re.search(r"^\*\*AI Delegation:\*\*\s*(.+)", body_clean, re.MULTILINE | re.IGNORECASE)
if m:
    print(m.group(1).strip())
    sys.exit(0)
sys.exit(0)
')

if [[ -z "$DELEG_VALUE" ]]; then
  fail "No AI Delegation field found in the PR body. Add '**AI Delegation:**' — see the PR template."
fi

echo "[AI-DELEG] Found delegation: '${DELEG_VALUE}'"

# ── 2. Reject the unedited placeholder ────────────────────────────────────────
if [[ "$DELEG_VALUE" == "<!-- fill in -->" ]] || \
   [[ "$DELEG_VALUE" == *"<!-- fill in -->"* ]] || \
   [[ "$DELEG_VALUE" == "fill in" ]]; then
  fail "AI Delegation field still contains the placeholder. Describe how AI was (or wasn't) used."
fi

# ── 3. N/A with a reason is always acceptable ─────────────────────────────────
if echo "$DELEG_VALUE" | grep -qiE '^N/A[[:space:]]*[-—]'; then
  REASON=$(echo "$DELEG_VALUE" | sed -E 's/^N\/A[[:space:]]*[-—][[:space:]]*//')
  if [[ -z "$REASON" ]]; then
    fail "N/A requires a reason, e.g. 'N/A — live pair session, no agent in the loop'."
  fi
  # Still require Prompt owner even on N/A changes — someone owns the workflow
  pass "AI Delegation N/A accepted — reason: ${REASON}"
  # Fall through to Prompt owner check
else
  pass "AI Delegation documented: '${DELEG_VALUE}'"
fi

# ── 4. Extract **Prompt owner:** value ────────────────────────────────────────
OWNER_VALUE=$(python3 -c '
import os, re, sys

body = os.environ.get("PR_BODY", "")
body_clean = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)

m = re.search(r"^\*\*Prompt owner:\*\*\s*(.+)", body_clean, re.MULTILINE | re.IGNORECASE)
if m:
    print(m.group(1).strip())
    sys.exit(0)
sys.exit(0)
')

if [[ -z "$OWNER_VALUE" ]]; then
  fail "No Prompt owner field found. Add '**Prompt owner:** @<handle>' to name who owns AI prompt iteration for this squad."
fi

echo "[AI-DELEG] Found prompt owner: '${OWNER_VALUE}'"

# ── 5. Reject placeholder prompt owner ────────────────────────────────────────
if [[ "$OWNER_VALUE" == "<!-- @handle -->" ]] || \
   [[ "$OWNER_VALUE" == "@handle" ]] || \
   [[ "$OWNER_VALUE" == *"<!-- "@"handle -->"* ]]; then
  fail "Prompt owner field still contains the placeholder. Replace with a real @handle (e.g. @lin, @amara, @jessica)."
fi

# ── 6. Validate @handle format ────────────────────────────────────────────────
if ! echo "$OWNER_VALUE" | grep -qE '^@[a-zA-Z0-9._-]+$'; then
  fail "Prompt owner '${OWNER_VALUE}' is not a valid @handle. Use the format @<github-username>."
fi

pass "Prompt owner validated: ${OWNER_VALUE}"
exit 0
