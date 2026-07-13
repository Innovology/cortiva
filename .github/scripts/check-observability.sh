#!/usr/bin/env bash
# Observability gate — RB-001 §7 (Innovology/cortiva)
#
# Three gates, in order:
#
#   G1 — PR body declares an Observability Impact section when the PR
#        touches service code under src/cortiva/.
#
#   G2 — No new FabricEvent or emit_simple calls in the diff are missing
#        a trace_id argument.  The observability pipeline (RB-001 §5.5)
#        requires every structured event to carry trace context so incident
#        chains can be correlated end-to-end.
#
#   G3 — No new httpx request calls (post/get/put/patch/delete/request) are
#        added to src/ without a traceparent or X-Trace-ID header.
#        Uncorrelated HTTP calls across service boundaries are a known gap;
#        this gate prevents the gap from widening.
#
# Inputs (environment variables):
#   PR_BODY       — full text of the pull request body
#   PR_NUMBER     — pull request number
#   REPO          — "owner/repo" string
#   GH_TOKEN      — GitHub token with repo scope (pull-requests: read)
#
# Exit codes:
#   0  all applicable gates pass
#   1  one or more gates fail

set -euo pipefail

PR_BODY="${PR_BODY:-}"
PR_NUMBER="${PR_NUMBER:-}"
REPO="${REPO:-}"
GH_TOKEN="${GH_TOKEN:-}"

RED='\033[0;31m'
YEL='\033[1;33m'
GRN='\033[0;32m'
NC='\033[0m'

fail()  { echo -e "${RED}[OBS GATE] FAIL: $*${NC}" >&2; GATE_FAIL=1; }
warn()  { echo -e "${YEL}[OBS GATE] WARN: $*${NC}"; }
pass()  { echo -e "${GRN}[OBS GATE] PASS: $*${NC}"; }
info()  { echo "[OBS GATE] $*"; }

GATE_FAIL=0

# ── Fetch the PR diff ────────────────────────────────────────────────────────
if [[ -z "$PR_NUMBER" || -z "$REPO" || -z "$GH_TOKEN" ]]; then
  fail "Missing required env vars (PR_NUMBER, REPO, GH_TOKEN). Cannot fetch diff."
  exit 1
fi

DIFF=$(curl -s \
  -H "Authorization: token ${GH_TOKEN}" \
  -H "Accept: application/vnd.github.v3.diff" \
  "https://api.github.com/repos/${REPO}/pulls/${PR_NUMBER}")

if [[ -z "$DIFF" ]]; then
  fail "Empty diff returned from GitHub API. Token may lack pull-requests:read scope."
  exit 1
fi

# Write diff to a temp file so Python inline scripts can read it without
# hitting shell argument size limits.
DIFF_FILE=$(mktemp)
echo "$DIFF" > "$DIFF_FILE"
export _OBS_DIFF_FILE="$DIFF_FILE"
trap 'rm -f "$DIFF_FILE"' EXIT

# ── Determine whether service code is touched ────────────────────────────────
TOUCHES_SRC=0
if echo "$DIFF" | grep -qE '^diff --git a/src/cortiva/'; then
  TOUCHES_SRC=1
fi

# ── G1 — Observability Impact section in PR body ────────────────────────────
echo ""
echo "=== G1: Observability Impact declaration ==="

if [[ $TOUCHES_SRC -eq 0 ]]; then
  pass "G1 — PR does not touch src/cortiva/; Observability Impact section not required."
else
  OBS_VALUE=$(python3 -c '
import os, re, sys

body = os.environ.get("PR_BODY", "")

m = re.search(
    r"^##\s+Observability\s+Impact\s*\n([\s\S]*?)(?=\n##\s|\Z)",
    body, re.MULTILINE | re.IGNORECASE
)
if not m:
    sys.exit(0)

section = m.group(1)
section = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL)
for line in section.splitlines():
    line = line.strip()
    if line:
        print(line)
        sys.exit(0)
sys.exit(0)
')

  if [[ -z "$OBS_VALUE" ]]; then
    fail "G1 — PR touches src/cortiva/ but has no '## Observability Impact' section in the body. Add the section and declare the impact, or write 'N/A — <reason>' if this change adds no new event emissions or service boundaries."
  elif echo "$OBS_VALUE" | grep -qiE '^N/A[[:space:]]*[-—]'; then
    REASON=$(echo "$OBS_VALUE" | sed -E 's/^N\/A[[:space:]]*[-—][[:space:]]*//')
    if [[ -z "$REASON" ]]; then
      fail "G1 — 'N/A' in Observability Impact requires a reason, e.g. 'N/A — no new event emissions or service boundaries'."
    else
      pass "G1 — N/A accepted. Reason: ${REASON}"
    fi
  elif echo "$OBS_VALUE" | grep -qiE '^N/A$'; then
    fail "G1 — Bare 'N/A' is not acceptable. Provide a reason: 'N/A — <reason>'."
  elif echo "$OBS_VALUE" | grep -qi "fill in\|placeholder"; then
    fail "G1 — Observability Impact section contains the placeholder text. Fill it in."
  else
    pass "G1 — Observability Impact declared: ${OBS_VALUE:0:120}"
  fi
fi

# ── G2 — New event emissions must carry trace_id ────────────────────────────
echo ""
echo "=== G2: Trace context on new event emissions ==="

# Collect added lines (+ prefix, not +++ file header) in src/cortiva/
# that call emit_simple( or FabricEvent( without a trace_id= argument.
MISSING_TRACE=$(python3 - <<'PYEOF'
import re, sys, os

diff_file = os.environ.get("_OBS_DIFF_FILE", "")
diff_text = open(diff_file).read() if diff_file and os.path.exists(diff_file) else ""

if not diff_text:
    sys.exit(0)

# Track whether we are inside a src/cortiva/ hunk
in_src = False
violations = []
current_file = ""

for line in diff_text.splitlines():
    if line.startswith("diff --git"):
        in_src = "src/cortiva/" in line
        m = re.search(r'b/(src/cortiva/\S+)', line)
        current_file = m.group(1) if m else ""
        continue
    if not in_src:
        continue
    if not line.startswith("+") or line.startswith("+++"):
        continue
    added = line[1:]  # Strip leading +

    # emit_simple( call without trace_id= on the same line
    if re.search(r'emit_simple\s*\(', added):
        if "trace_id=" not in added:
            violations.append(f"  {current_file}: emit_simple() call missing trace_id= — {added.strip()[:100]}")

    # FabricEvent( instantiation without trace_id= on the same line
    if re.search(r'FabricEvent\s*\(', added):
        if "trace_id=" not in added:
            violations.append(f"  {current_file}: FabricEvent() missing trace_id= — {added.strip()[:100]}")

for v in violations:
    print(v)
PYEOF
)

if [[ -n "$MISSING_TRACE" ]]; then
  fail "G2 — New event emissions without trace_id detected. Each FabricEvent / emit_simple call must carry a trace_id so incident chains can be correlated (RB-001 §5.5). Either pass the trace_id from the calling context, or generate one with uuid.uuid4().hex and propagate it through the operation:"
  echo "$MISSING_TRACE" >&2
else
  pass "G2 — All new event emissions in the diff carry trace_id (or none were added)."
fi

# ── G3 — New HTTP calls must carry trace headers ─────────────────────────────
echo ""
echo "=== G3: Trace headers on new HTTP calls ==="

MISSING_HEADERS=$(python3 - <<'PYEOF'
import re, sys, os

diff_file = os.environ.get("_OBS_DIFF_FILE", "")
diff_text = open(diff_file).read() if diff_file and os.path.exists(diff_file) else ""
if not diff_text:
    sys.exit(0)

in_src = False
violations = []
current_file = ""
# Buffer added lines to allow lookahead (headers may be on adjacent lines)
added_block: list[str] = []
LOOKAHEAD = 8  # lines to scan after a request call for header evidence

lines = diff_text.splitlines()
i = 0
while i < len(lines):
    line = lines[i]
    if line.startswith("diff --git"):
        in_src = "src/cortiva/" in line
        m = re.search(r'b/(src/cortiva/\S+)', line)
        current_file = m.group(1) if m else ""
        i += 1
        continue
    if not in_src:
        i += 1
        continue
    if not line.startswith("+") or line.startswith("+++"):
        i += 1
        continue

    added = line[1:]

    # Detect new httpx request call sites
    if re.search(r'\.(get|post|put|patch|delete|request)\s*\(', added, re.IGNORECASE):
        # Gather context window of added lines around this call
        window = []
        for j in range(max(0, i - LOOKAHEAD), min(len(lines), i + LOOKAHEAD + 1)):
            if lines[j].startswith("+") and not lines[j].startswith("+++"):
                window.append(lines[j][1:])
        context = "\n".join(window)
        has_trace = bool(re.search(
            r'traceparent|tracestate|x-trace-id|X-Trace-ID|trace_id|headers\s*=',
            context, re.IGNORECASE
        ))
        if not has_trace:
            violations.append(
                f"  {current_file}: new HTTP request call without trace header evidence — "
                f"{added.strip()[:100]}"
            )
    i += 1

for v in violations:
    print(v)
PYEOF
)

if [[ -n "$MISSING_HEADERS" ]]; then
  fail "G3 — New HTTP request calls detected without traceparent/X-Trace-ID header evidence. Outbound HTTP calls across service boundaries must propagate trace context (RB-001 §5.5). Add a 'traceparent' header constructed from the current trace_id and a new span_id, or pass headers={'traceparent': f'00-{trace_id}-{span_id}-01'}:"
  echo "$MISSING_HEADERS" >&2
else
  pass "G3 — No new uncorrelated HTTP calls detected (or none added)."
fi

# ── Final verdict ─────────────────────────────────────────────────────────────
echo ""
echo "=== Observability gate summary ==="
if [[ $GATE_FAIL -eq 0 ]]; then
  pass "All observability gates green. See RB-001 §7 for the full gate spec."
  exit 0
else
  fail "One or more observability gates failed. Fix the issues above before merging. See RB-001 §7 and sre-head/workspace/runbooks/RB-001-observability-pipeline-failure.md for context."
  exit 1
fi
