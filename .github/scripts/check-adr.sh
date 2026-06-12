#!/usr/bin/env bash
# Validates the ADR/RFC reference field in a PR body.
# Exits 1 if the reference is missing, is the placeholder, or names a wiki page
# that does not exist in this repo's wiki.
#
# Supports two PR template formats used across Innovology repos:
#   Inline bold:     **ADR/RFC:** <value>
#   Section heading: ## ADR / RFC Reference\n\n<value>

set -euo pipefail

PR_BODY="${PR_BODY:-}"
REPO="${REPO:-}"
GH_TOKEN="${GH_TOKEN:-}"

RED='\033[0;31m'
YEL='\033[1;33m'
GRN='\033[0;32m'
NC='\033[0m'

fail()  { echo -e "${RED}[ADR LINT] FAIL: $*${NC}" >&2; exit 1; }
warn()  { echo -e "${YEL}[ADR LINT] WARN: $*${NC}"; }
pass()  { echo -e "${GRN}[ADR LINT] PASS: $*${NC}"; }

# ── 1. Extract the ADR/RFC value ─────────────────────────────────────────────
# Supports two formats used across Innovology PR templates:
#   Inline bold:     **ADR/RFC:** <value>          (legacy format)
#   Section heading: ## ADR / RFC Reference        (current template format)
#                    <value on next non-blank line>

ADR_VALUE=$(python3 -c '
import os, re, sys

body = os.environ.get("PR_BODY", "")

# Format 1: inline bold  **ADR/RFC:** value
m = re.search(r"^\*\*ADR/RFC\*\*:\s*(.+)", body, re.MULTILINE | re.IGNORECASE)
if m:
    print(m.group(1).strip())
    sys.exit(0)

# Format 2: section heading  ## ADR / RFC Reference (spaces around / are optional)
m = re.search(
    r"^##\s+ADR\s*/\s*RFC\s+Reference\s*\n([\s\S]*?)(?=\n##\s|\Z)",
    body, re.MULTILINE | re.IGNORECASE
)
if m:
    section = m.group(1)
    # Strip HTML comments (<!-- ... -->)
    section = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL)
    for line in section.splitlines():
        line = line.strip()
        if line:
            print(line)
            sys.exit(0)

# Nothing found — empty output signals missing field
sys.exit(0)
')

if [[ -z "$ADR_VALUE" ]]; then
  fail "No ADR/RFC reference found in the PR body. Add an '## ADR / RFC Reference' section (or '**ADR/RFC:**' field) — see the PR template."
fi

echo "[ADR LINT] Found reference: '${ADR_VALUE}'"

# ── 2. Reject the unedited placeholder ──────────────────────────────────────
if [[ "$ADR_VALUE" == "<!-- fill in -->" ]] || [[ "$ADR_VALUE" == *"<!-- fill in -->"* ]]; then
  fail "ADR/RFC field still contains the placeholder. Replace it with a real reference or 'N/A — <reason>'."
fi

# ── 3. N/A with a reason is always acceptable ───────────────────────────────
if echo "$ADR_VALUE" | grep -qiE '^N/A[[:space:]]*[-—]'; then
  REASON=$(echo "$ADR_VALUE" | sed -E 's/^N\/A[[:space:]]*[-—][[:space:]]*//')
  if [[ -z "$REASON" ]]; then
    fail "N/A requires a reason, e.g. 'N/A — dependency version bump'."
  fi
  pass "N/A accepted — reason: ${REASON}"
  exit 0
fi

# Bare "N/A" without a reason is rejected
if echo "$ADR_VALUE" | grep -qiE '^N/A$'; then
  fail "Bare 'N/A' is not acceptable. Provide a reason: 'N/A — <reason>'."
fi

# ── 4. Resolve the reference to a wiki page slug ────────────────────────────
# Supported forms:
#   [[ADR-0023-auth-strategy]]           → wiki page ADR-0023-auth-strategy
#   ADR-0023                             → wiki page prefix search
#   https://github.com/…/wiki/ADR-0023-… → direct URL, extract slug
#   https://github.com/…/wiki/Home       → any full wiki URL

SLUG=""

if [[ "$ADR_VALUE" =~ ^\[\[(.+)\]\]$ ]]; then
  # Wiki link syntax [[Page-Name]]
  SLUG="${BASH_REMATCH[1]}"
elif [[ "$ADR_VALUE" =~ github\.com/.*/wiki/([^/[:space:]]+) ]]; then
  # Full URL — extract the slug after /wiki/
  SLUG="${BASH_REMATCH[1]}"
  # Decode %20 etc.
  SLUG=$(python3 -c "import sys, urllib.parse; print(urllib.parse.unquote(sys.argv[1]))" "$SLUG" 2>/dev/null || echo "$SLUG")
elif echo "$ADR_VALUE" | grep -qiE '^(ADR|RFC)-[0-9]'; then
  # Bare identifier like ADR-0023 or RFC-004
  SLUG="$ADR_VALUE"
else
  fail "Cannot parse ADR/RFC reference '${ADR_VALUE}'. Use [[ADR-XXXX-slug]], a full wiki URL, a bare ADR-/RFC- identifier, or 'N/A — <reason>'."
fi

echo "[ADR LINT] Resolved slug: '${SLUG}'"

# ── 5. Check the wiki ────────────────────────────────────────────────────────
WIKI_DIR=$(mktemp -d)
WIKI_URL="https://x-access-token:${GH_TOKEN}@github.com/${REPO}.wiki.git"

if ! git clone --depth 1 --quiet "$WIKI_URL" "$WIKI_DIR" 2>/dev/null; then
  warn "Wiki does not exist or is empty for ${REPO}. Cannot verify ADR reference '${SLUG}'."
  warn "Create the wiki and add the ADR page before merging."
  fail "Wiki not found for ${REPO}. Publish the ADR to the wiki first, then re-run CI."
fi

# Search for a matching file — exact match first, then prefix/substring
MATCH=$(find "$WIKI_DIR" -maxdepth 1 -name "*.md" \
  | sed 's|.*/||; s|\.md$||' \
  | grep -iF "$SLUG" \
  | head -1 || true)

rm -rf "$WIKI_DIR"

if [[ -z "$MATCH" ]]; then
  fail "Wiki page matching '${SLUG}' not found in ${REPO} wiki. Create the ADR page before merging, or use 'N/A — <reason>' if no ADR is needed."
fi

pass "ADR/RFC reference '${SLUG}' resolved to wiki page '${MATCH}'."
exit 0
