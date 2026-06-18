# Using HARIS (the org directory)

Your HARIS key and the API origin arrive in your environment as `HARIS_API_KEY`
and `HARIS_BASE_URL`. All endpoints live under `{HARIS_BASE_URL}/api/v1` and
authenticate with the header `x-api-key: $HARIS_API_KEY`.

The reliable way to call HARIS is the bundled client (no flags to get wrong):

```python
from cortiva.skills.haris_directory.client import HarisClient, HarisError

haris = HarisClient()              # reads HARIS_API_KEY / HARIS_BASE_URL from env
if not haris.configured:
    ...                            # no key yet — escalate, don't fabricate

# Who is in the org? (agents, people, or both)
who = haris.list_directory(search="finance")        # -> {"agents": [...], "humans": [...]}

# Register a newly-hired agent (needs a manager+ key)
haris.provision_agent(name="Vera", kind="assistant", department="AR", model="qwen3.6-35b")

# Create or update a person (create needs hr; update needs manager+)
haris.upsert_human(first_name="Alex", last_name="Browne",
                   email="alex@innovology.io", title="Founder")
```

Or call the REST API directly:

```bash
curl -s -H "x-api-key: $HARIS_API_KEY" \
  "$HARIS_BASE_URL/api/v1/agents?q=finance&pageSize=20"          # list agents
curl -s -H "x-api-key: $HARIS_API_KEY" "$HARIS_BASE_URL/api/v1/humans?q=alex"  # list people
```

List responses are `{ "data": [...], "pagination": {...} }`.

## Rules
- **Don't fabricate org members.** HARIS is the authority. If someone isn't in
  it, they aren't in the org — look them up, don't assume.
- **Respect RBAC.** A `403` means your key's role is too low for that write
  (viewer/member can only read). Escalate to the directory owner; do not loop.
- **`provision_agent` derives the slug** from the name server-side; a duplicate
  returns `409` — the record already exists, so list first if unsure.
- **`upsert_human` is create-or-update** keyed on email: it POSTs a new person,
  and on a `409` (email already exists) finds them and PATCHes instead.
- Valid agent `kind` values: assistant, autonomous, workflow, copilot, service,
  other.
