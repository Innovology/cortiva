# HARIS — the org directory (people & agents)

HARIS (Human-Agent-Resource-Information-System) is Innovology's identity hub at
`haris.innovology.io` — the single source of truth for **who exists in the
org**: every employee and every AI agent, their roles, departments, and
reporting lines. It is separate from Cortiva: your *identity* lives in HARIS,
your *runtime* lives on the Cortiva node.

You hold your **own** HARIS API key (delivered to your node as `HARIS_API_KEY`,
with the API origin in `HARIS_BASE_URL`). Every call you make is attributed to
you, and HARIS enforces role-based access per identity:

- **viewer / member** — read the directory.
- **manager** — also create/update agent records and update people.
- **hr** — also create people.
- **admin** — full control (the directory owner).

If a write returns `403`, your key's role is too low for that action — ask the
directory owner rather than retrying. If a read returns `401`, your key isn't
set up yet.

Use HARIS when you need to know who's in the org, find a colleague's
department/manager, register a newly-hired agent, or keep a person's record
current. Do not invent org members — HARIS is the authority; if someone isn't
in HARIS, they aren't in the org.
