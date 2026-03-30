#!/usr/bin/env python3
"""
Sync the MCP server registry into Cortiva's skill registry.

Fetches all servers from registry.modelcontextprotocol.io and
converts them into Cortiva skill entries.  The output is written
to src/cortiva/skills/registry.yaml.

Usage:
    python3 scripts/sync_mcp_registry.py
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from typing import Any
from pathlib import Path

API_BASE = "https://registry.modelcontextprotocol.io/v0.1/servers"
PAGE_SIZE = 100
OUTPUT = Path(__file__).parent.parent / "src" / "cortiva" / "skills" / "registry.yaml"

# Category inference from name/description keywords
CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("project-management", ["jira", "linear", "asana", "trello", "monday", "clickup", "notion", "todoist", "basecamp", "shortcut", "height", "kanban", "sprint", "backlog", "task management", "issue track"]),
    ("version-control", ["gitlab", "bitbucket", "git repo", "git commit", "pull request", "merge request", "version control", "code review"]),
    ("databases", ["postgres", "mysql", "sqlite", "mongodb", "redis", "dynamodb", "supabase", "firebase", "prisma", "neon", "planetscale", "elasticsearch", "pinecone", "weaviate", "qdrant", "chroma", "database", "sql", "nosql", "vector db"]),
    ("cloud-infrastructure", ["aws", "gcp", "azure", "cloudflare", "vercel", "netlify", "terraform", "kubernetes", "docker", "digitalocean", "fly.io", "railway", "render", "heroku", "cloud deploy", "infrastructure"]),
    ("monitoring", ["datadog", "grafana", "sentry", "pagerduty", "prometheus", "new relic", "opsgenie", "honeycomb", "monitoring", "observability", "alerting", "apm", "logging"]),
    ("communication", ["slack", "discord", "teams", "telegram", "twilio", "messaging", "chat", "notification"]),
    ("email", ["email", "sendgrid", "mailgun", "gmail", "outlook", "smtp", "imap"]),
    ("browser-automation", ["browser", "puppeteer", "playwright", "selenium", "chrome", "headless", "scraping", "crawl"]),
    ("search", ["brave search", "google search", "exa search", "tavily", "serper", "web search", "search engine", "search api"]),
    ("code-quality", ["lint", "eslint", "prettier", "sonar", "code quality", "static analysis", "formatting"]),
    ("testing", ["test", "jest", "pytest", "cypress", "playwright test", "k6", "load test", "e2e"]),
    ("security", ["vault", "snyk", "trivy", "semgrep", "security scan", "vulnerability", "secret"]),
    ("analytics", ["analytics", "mixpanel", "amplitude", "posthog", "segment", "google analytics"]),
    ("crm", ["salesforce", "hubspot", "pipedrive", "intercom", "zendesk", "freshdesk", "crm", "customer"]),
    ("finance", ["stripe", "plaid", "quickbooks", "xero", "payment", "invoice", "billing", "banking"]),
    ("documentation", ["confluence", "google docs", "google sheets", "airtable", "coda", "wiki", "documentation"]),
    ("storage", ["s3", "google drive", "dropbox", "box", "onedrive", "cloud storage", "file storage"]),
    ("ai-ml", ["openai", "anthropic", "hugging", "replicate", "stability", "langchain", "embedding", "llm", "machine learning", "neural"]),
    ("design", ["figma", "canva", "sketch", "design", "ui design"]),
    ("devops", ["ci/cd", "github actions", "circleci", "jenkins", "argocd", "ansible", "pipeline", "deploy"]),
    ("marketing", ["mailchimp", "google ads", "facebook ads", "marketing", "campaign", "advertising"]),
    ("media", ["youtube", "spotify", "cloudinary", "video", "audio", "image process", "media"]),
    ("ecommerce", ["shopify", "woocommerce", "ecommerce", "product", "order", "cart"]),
    ("calendar", ["calendar", "scheduling", "appointment", "booking"]),
    ("productivity", ["productivity", "time", "weather", "maps", "translation", "calculator"]),
    ("data-processing", ["dbt", "snowflake", "bigquery", "databricks", "data pipeline", "etl", "data transform"]),
    ("blockchain", ["blockchain", "crypto", "web3", "ethereum", "solana", "nft", "defi", "trading"]),
    ("iot", ["iot", "mqtt", "home assistant", "sensor", "device", "smart home"]),
    ("api-integration", ["api", "rest", "graphql", "webhook", "zapier", "make", "integration", "automation"]),
]


def infer_category(name: str, description: str) -> str:
    """Infer a category from the server name and description.

    Only matches against name + description text, NOT repository URLs
    or other metadata that would cause false positives (e.g. every
    server with a GitHub repo being categorised as version-control).
    """
    text = f"{name} {description}".lower()
    for category, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in text:
                return category
    return "other"


def sanitize_name(name: str) -> str:
    """Convert an MCP registry name to a Cortiva skill name."""
    # e.g. "com.example/my-server" → "example-my-server"
    # Remove common prefixes
    name = re.sub(r"^(com|io|ai|dev|org|net)\.", "", name)
    # Replace / with -
    name = name.replace("/", "-")
    # Remove mcp-server prefix/suffix
    name = re.sub(r"^mcp-server-?", "", name)
    name = re.sub(r"-?mcp-server$", "", name)
    name = re.sub(r"-?mcp$", "", name)
    # Clean up
    name = re.sub(r"[^a-z0-9-]", "-", name.lower())
    name = re.sub(r"-+", "-", name).strip("-")
    return name or "unknown"


def extract_command(server: dict[str, Any]) -> str:
    """Extract the best command to run this MCP server."""
    packages = server.get("packages") or []
    for pkg in packages:
        registry = pkg.get("registryType") or pkg.get("registry_name") or ""
        pkg_name = pkg.get("identifier") or pkg.get("name") or ""
        if "npm" in registry.lower() and pkg_name:
            return f"npx -y {pkg_name}"
        if "pypi" in registry.lower() and pkg_name:
            return f"uvx {pkg_name}"

    # Fallback: try remotes
    remotes = server.get("remotes") or []
    for remote in remotes:
        url = remote.get("url", "")
        if url:
            return f"# Remote: {url}"

    return ""


def extract_env_vars(server: dict[str, Any]) -> list[str]:
    """Extract required environment variables."""
    env_vars: list[str] = []

    # From packages
    for pkg in server.get("packages") or []:
        for ev in pkg.get("environmentVariables") or pkg.get("environment_variables") or []:
            name = ev.get("name", "")
            if name and ev.get("isRequired", ev.get("is_required", False)):
                env_vars.append(name)

    # From remotes
    for remote in server.get("remotes") or []:
        for header in remote.get("headers") or []:
            name = header.get("name", "")
            if name and header.get("isRequired", False):
                env_vars.append(f"{name}_HEADER")

    return list(dict.fromkeys(env_vars))  # dedupe preserving order


def extract_package_name(server: dict[str, Any]) -> str:
    """Extract the package name."""
    for pkg in server.get("packages") or []:
        name = pkg.get("identifier") or pkg.get("name") or ""
        if name:
            return name
    return ""


def fetch_all_servers() -> list[dict[str, Any]]:
    """Fetch all servers from the MCP registry via pagination."""
    all_servers: list[dict[str, Any]] = []
    cursor = None
    page = 0

    while True:
        page += 1
        url = f"{API_BASE}?limit={PAGE_SIZE}"
        if cursor:
            url += f"&cursor={urllib.request.quote(cursor)}"

        print(f"  Fetching page {page}... ({len(all_servers)} so far)", flush=True)

        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"  Error fetching page {page}: {e}")
            break

        servers = data.get("servers", [])
        if not servers:
            break

        all_servers.extend(servers)

        metadata = data.get("metadata", {})
        cursor = metadata.get("nextCursor")
        if not cursor:
            break

        # Rate limiting — be polite
        time.sleep(0.2)

    return all_servers


def convert_to_skill(server: dict[str, Any]) -> dict[str, Any] | None:
    """Convert an MCP registry server to a Cortiva skill entry."""
    name = server.get("name", "")
    description = server.get("description", "")
    if not name:
        return None

    skill_name = sanitize_name(name)
    category = infer_category(name, description)
    command = extract_command(server)
    env_vars = extract_env_vars(server)
    package = extract_package_name(server)

    entry: dict[str, Any] = {
        "name": skill_name,
        "description": description[:120] if description else "",
        "category": category,
        "version": server.get("version", "1.0"),
    }

    if command or package:
        mcp: dict[str, Any] = {}
        if package:
            mcp["package"] = package
        if command:
            mcp["command"] = command
        if env_vars:
            mcp["env"] = env_vars
        entry["mcp"] = mcp

    return entry


def write_registry(skills: list[dict[str, Any]]) -> None:
    """Write the registry YAML file."""
    # Group by category
    by_category: dict[str, list[dict[str, Any]]] = {}
    for skill in skills:
        cat = skill.get("category", "other")
        by_category.setdefault(cat, []).append(skill)

    lines = [
        "# Cortiva Skill Registry",
        "#",
        f"# Auto-generated from registry.modelcontextprotocol.io",
        f"# Total: {len(skills)} skills across {len(by_category)} categories",
        "#",
        "# Install with: cortiva skill install <name> --agent <agent-id>",
        "",
        "skills:",
    ]

    for category in sorted(by_category.keys()):
        cat_skills = sorted(by_category[category], key=lambda s: s["name"])
        lines.append("")
        lines.append(f"  # {'=' * 65}")
        lines.append(f"  # {category.upper().replace('-', ' ')}")
        lines.append(f"  # {'=' * 65}")

        for skill in cat_skills:
            lines.append("")
            lines.append(f"  - name: {skill['name']}")
            if skill.get("description"):
                # Escape YAML special chars
                desc = skill["description"].replace('"', '\\"')
                lines.append(f'    description: "{desc}"')
            lines.append(f"    category: {skill['category']}")
            if skill.get("version"):
                lines.append(f'    version: "{skill["version"]}"')

            mcp = skill.get("mcp")
            if mcp:
                lines.append("    mcp:")
                if mcp.get("package"):
                    lines.append(f'      package: "{mcp["package"]}"')
                if mcp.get("command"):
                    lines.append(f'      command: "{mcp["command"]}"')
                if mcp.get("env"):
                    lines.append(f"      env: [{', '.join(mcp['env'])}]")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    print("Syncing MCP registry → Cortiva skill registry")
    print()

    # Fetch all servers
    print("Fetching from registry.modelcontextprotocol.io...")
    servers = fetch_all_servers()
    print(f"  Fetched {len(servers)} servers total")
    print()

    # Convert to skills
    print("Converting to Cortiva skills...")
    skills: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for raw in servers:
        # API wraps each entry in {"server": {...}, "_meta": {...}}
        server = raw.get("server", raw) if isinstance(raw, dict) else raw
        entry = convert_to_skill(server)
        if entry is None:
            continue
        # Deduplicate by name
        if entry["name"] in seen_names:
            # Append a suffix
            entry["name"] = f"{entry['name']}-{entry.get('version', '').replace('.', '')}"
        if entry["name"] in seen_names:
            continue
        seen_names.add(entry["name"])
        skills.append(entry)

    print(f"  Converted {len(skills)} skills")

    # Category stats
    cats: dict[str, int] = {}
    for s in skills:
        cat = s.get("category", "other")
        cats[cat] = cats.get(cat, 0) + 1

    print(f"  Categories: {len(cats)}")
    for cat in sorted(cats.keys()):
        print(f"    {cat:<30} {cats[cat]:>5}")
    print()

    # Write
    print(f"Writing to {OUTPUT}...")
    write_registry(skills)
    print(f"  Done. {len(skills)} skills written.")


if __name__ == "__main__":
    main()
