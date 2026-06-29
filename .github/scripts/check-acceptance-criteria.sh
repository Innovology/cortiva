#!/usr/bin/env bash
# Validates the Acceptance Criteria section in a PR body.
#
# Rules:
#   1. Section "## Acceptance Criteria" must be present.
#   2. After stripping HTML comments, at least one "- [ ] <non-placeholder text>"
#      line must exist.
#   3. All criteria lines must have actual content (not just the placeholder
#      comment or whitespace).
#
# Exempt: Dependabot PRs (checked via GITHUB_ACTOR env var).
# Called by: acceptance-criteria-lint.yml  OR  pr-lint.yml

set -euo pipefail

PR_BODY="${PR_BODY:-}"
GITHUB_ACTOR="${GITHUB_ACTOR:-}"

RED='\033[0;31m'
GRN='\033[0;32m'
YEL='\033[1;33m'
NC='\033[0m'

fail() { echo -e "${RED}[AC LINT] FAIL: $*${NC}" >&2; exit 1; }
warn() { echo -e "${YEL}[AC LINT] WARN: $*${NC}"; }
pass() { echo -e "${GRN}[AC LINT] PASS: $*${NC}"; }

# ── 0. Bot exemptions ────────────────────────────────────────────────────────
BOT_ACTORS="dependabot dependabot[bot] github-actions github-actions[bot]"
for bot in $BOT_ACTORS; do
  if [[ "${GITHUB_ACTOR}" == "$bot" ]]; then
    pass "Bot actor (${GITHUB_ACTOR}) — skipping Acceptance Criteria check."
    exit 0
  fi
done

# ── 1. Extract the Acceptance Criteria section ───────────────────────────────
SECTION=$(python3 -c '
import os, re, sys

body = os.environ.get("PR_BODY", "")

m = re.search(
    r"^##\s+Acceptance\s+Criteria\s*\n([\s\S]*?)(?=\n##\s|\Z)",
    body, re.MULTILINE | re.IGNORECASE
)
if not m:
    sys.exit(2)   # section missing

section = m.group(1)
# Strip HTML comments
section = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL)
print(section)
' 2>/dev/null)

EXIT=$?
if [[ $EXIT -eq 2 ]]; then
  fail "No '## Acceptance Criteria' section found in the PR body. Add the section and list at least one testable criterion."
fi

# ── 2. Require at least one real checkbox ────────────────────────────────────
# Extract "- [ ] <content>" lines (checked or unchecked).
CRITERIA=$(echo "$SECTION" | grep -E '^\s*-\s*\[[ xX]\]\s*.+' || true)

if [[ -z "$CRITERIA" ]]; then
  fail "The '## Acceptance Criteria' section has no checkbox items. Add at least one:
  - [ ] <testable criterion>
Example: '- [ ] POST /api/bets returns 422 when stake exceeds maxBetAmount'"
fi

# ── 3. Reject lines that are empty or only whitespace after stripping ─────────
BLANK_COUNT=0
while IFS= read -r line; do
  # Extract the text after "- [ ] " or "- [x] "
  content=$(echo "$line" | sed -E 's/^\s*-\s*\[[ xX]\]\s*//')
  if [[ -z "${content// /}" ]]; then
    BLANK_COUNT=$((BLANK_COUNT + 1))
  fi
done <<< "$CRITERIA"

if [[ $BLANK_COUNT -gt 0 ]]; then
  fail "$BLANK_COUNT acceptance criterion/criteria line(s) are blank after the checkbox. Every criterion must have content:
  ✅  - [ ] Users cannot place a bet when the market is closed
  ❌  - [ ]    (blank)"
fi

# ── 4. Reject the unedited placeholder ───────────────────────────────────────
if echo "$CRITERIA" | grep -qiE 'criterion[[:space:]]*:'; then
  fail "One or more criteria still contain the placeholder text ('criterion: ...'). Replace with actual verifiable criteria."
fi

# ── 5. Summary ───────────────────────────────────────────────────────────────
COUNT=$(echo "$CRITERIA" | wc -l | tr -d ' ')
pass "${COUNT} acceptance criterion/criteria found and validated."
exit 0
