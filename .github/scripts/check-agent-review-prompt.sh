#!/usr/bin/env bash
# Validates the Agent Review Prompt block in a PR body.
#
# Schema: schemas/agent-review-prompt.schema.json (canonical spec)
#
# Rules enforced:
#   1. If AI Delegation is N/A, the block is optional — exit 0.
#   2. If AI Delegation is filled (non-N/A), the block is REQUIRED.
#   3. **Change summary:** must be present, non-placeholder, >= 20 chars.
#   4. **Risks:** must be present, non-placeholder, contain a known category
#      or explicit "none" with a reason.
#   5. **Human review focus:** must be present, non-placeholder, >= 20 chars.
#
# Exempt: Dependabot and bot actors (checked via GITHUB_ACTOR env var).
# Called by: agent-review-prompt-lint.yml  OR  pr-lint.yml

set -euo pipefail

PR_BODY="${PR_BODY:-}"
GITHUB_ACTOR="${GITHUB_ACTOR:-}"

RED='\033[0;31m'
GRN='\033[0;32m'
NC='\033[0m'

fail() { echo -e "${RED}[ARP LINT] FAIL: $*${NC}" >&2; exit 1; }
pass() { echo -e "${GRN}[ARP LINT] PASS: $*${NC}"; }

# ── 0. Bot exemptions ─────────────────────────────────────────────────────────
BOT_ACTORS="dependabot dependabot[bot] github-actions github-actions[bot]"
for bot in $BOT_ACTORS; do
  if [[ "${GITHUB_ACTOR}" == "$bot" ]]; then
    pass "Bot actor (${GITHUB_ACTOR}) — skipping Agent Review Prompt check."
    exit 0
  fi
done
if echo "$PR_BODY" | grep -qE '^Bumps \['; then
  pass "Dependabot PR — Agent Review Prompt check skipped."
  exit 0
fi

# ── 1–5. Single Python pass ───────────────────────────────────────────────────
python3 - <<'PYEOF'
import os, re, sys

RED = "\033[0;31m"
GRN = "\033[0;32m"
NC  = "\033[0m"

def fail(msg):
    print(f"{RED}[ARP LINT] FAIL: {msg}{NC}", file=sys.stderr)
    sys.exit(1)

def pass_(msg):
    print(f"{GRN}[ARP LINT] PASS: {msg}{NC}")

PLACEHOLDER_PATTERNS = [
    r"<!--.*?-->",          # unedited HTML comment
    r"^\s*$",              # blank
    r"fill\s+in",          # "fill in" literal
    r"one\s+line[:\s]",    # template instruction text
]

KNOWN_RISKS = {"security", "money-path", "data-integrity", "backwards-compat", "performance", "none"}

def is_placeholder(text):
    stripped = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
    if not stripped:
        return True
    if re.search(r"\bfill\s+in\b", stripped, re.IGNORECASE):
        return True
    return False

def extract_field(body_clean, field_name):
    """Extract the value after **field_name:** on a line (same line only)."""
    # Use [ \t]* (horizontal whitespace) not \s* — \s includes \n which would
    # allow the capture group to slide onto the next line.
    pattern = rf"^\*\*{re.escape(field_name)}:\*\*[ \t]*(.*)"
    m = re.search(pattern, body_clean, re.MULTILINE | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None

body = os.environ.get("PR_BODY", "")
body_clean = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)

# ── Rule 1: Check AI Delegation to determine if ARP block is required ─────────
ai_deleg = extract_field(body_clean, "AI Delegation")

if ai_deleg is None:
    # AI Delegation field absent — this is caught by check-ai-delegation.sh,
    # but we treat ARP as required by default (conservative).
    pass
elif re.match(r"^N/A\s*[-—]", ai_deleg, re.IGNORECASE):
    pass_("AI Delegation is N/A — Agent Review Prompt block is optional. Skipping.")
    sys.exit(0)

# ── Rule 2: ARP block must be present ─────────────────────────────────────────
arp_match = re.search(
    r"^##\s+Agent\s+Review\s+Prompt\s*\n([\s\S]*?)(?=\n##\s|\Z)",
    body, re.MULTILINE | re.IGNORECASE
)
if not arp_match:
    fail(
        "No '## Agent Review Prompt' section found, but AI Delegation is not N/A.\n"
        "  When AI tooling is used, fill in the Agent Review Prompt block before\n"
        "  requesting human review. See schemas/agent-review-prompt.schema.json.\n"
        "  Example:\n"
        "    **Change summary:** Adds ESLint rule to catch invalid Next.js route exports\n"
        "    **Risks:** backwards-compat\n"
        "    **Human review focus:** Confirm the regex whitelist is exhaustive"
    )

arp_section = arp_match.group(1)
arp_clean = re.sub(r"<!--.*?-->", "", arp_section, flags=re.DOTALL)

# ── Rule 3: Change summary ─────────────────────────────────────────────────────
summary = extract_field(arp_clean, "Change summary")
if summary is None:
    fail(
        "Agent Review Prompt is missing '**Change summary:**'.\n"
        "  Add a one-line description of what this PR changes and why.\n"
        "  Example: 'Replaces randomUUID() client token with FNV-1a hash for stable idempotency'"
    )
if is_placeholder(summary):
    fail(
        f"'**Change summary:**' still contains placeholder text: {repr(summary)}\n"
        "  Replace with a one-line description specific to this change."
    )
if len(summary) < 20:
    fail(
        f"'**Change summary:**' is too short ({len(summary)} chars, min 20 — be specific): {repr(summary)}"
    )

# ── Rule 4: Risks ──────────────────────────────────────────────────────────────
risks_raw = extract_field(arp_clean, "Risks")
if risks_raw is None:
    fail(
        "Agent Review Prompt is missing '**Risks:**'.\n"
        "  Mark all that apply: security | money-path | data-integrity | backwards-compat | performance\n"
        "  Or use 'none — <reason>' if this PR has no risk surface."
    )
if is_placeholder(risks_raw):
    fail(
        f"'**Risks:**' still contains placeholder text: {repr(risks_raw)}\n"
        "  List applicable risk categories or write 'none — <reason>'."
    )

# Validate at least one recognized risk or 'none — reason'
risks_lower = risks_raw.lower()
has_known_risk = any(r in risks_lower for r in KNOWN_RISKS)
has_none_with_reason = bool(re.match(r"^none\s*[-—]\s*.{5,}", risks_lower))
if not has_known_risk and not has_none_with_reason:
    fail(
        f"'**Risks:**' value {repr(risks_raw)} doesn't match any known category.\n"
        "  Use one or more of: security | money-path | data-integrity | backwards-compat | performance\n"
        "  Or 'none — <reason>' if no risk categories apply."
    )

# ── Rule 5: Human review focus ─────────────────────────────────────────────────
focus = extract_field(arp_clean, "Human review focus")
if focus is None:
    fail(
        "Agent Review Prompt is missing '**Human review focus:**'.\n"
        "  Name what the reviewer should scrutinise after the agent pass.\n"
        "  Example: 'Confirm the lease release fires on all error paths in the split-leg loop'"
    )
if is_placeholder(focus):
    fail(
        f"'**Human review focus:**' still contains placeholder text: {repr(focus)}\n"
        "  Describe specifically what the reviewer should check."
    )
if len(focus) < 20:
    fail(
        f"'**Human review focus:**' is too short ({len(focus)} chars, min 20 — be specific): {repr(focus)}"
    )

pass_(f"Agent Review Prompt validated — change summary, risks ({risks_raw!r}), human review focus.")
PYEOF
