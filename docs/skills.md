# Skills

Skills are reusable capabilities that agents can acquire. A skill packages tool definitions, prompts, and configuration into a unit that can be searched, installed, and shared across agents.

## What Is a Skill

A skill is a directory containing:

```
skills/
  web-search/
    skill.yaml          # Metadata and configuration
    tools.json          # Tool definitions (MCP format)
    prompts/
      search.md         # Prompt templates
    README.md           # Human-readable docs
```

The `skill.yaml` defines the skill's identity:

```yaml
name: web-search
version: 1.0.0
description: Search the web using configurable search providers.
author: cortiva-team
tags:
  - search
  - web
  - research

requires:
  env:
    - SEARCH_API_KEY
  adapters:
    - consciousness

tools:
  - tools.json

prompts:
  - prompts/search.md
```

When a skill is installed on an agent, its tools become available during task execution and its prompts are included in context assembly.

## Searching for Skills

Skills are discovered from a registry. The default registry is the local `skills/` directory in the workspace. Additional registries can be configured:

```yaml
skills:
  registries:
    - path: ./skills                    # Local directory
    - url: https://registry.cortiva.dev # Remote registry (future)
```

Search from the CLI:

```bash
# List all available skills
cortiva skill list

# Search by tag
cortiva skill search --tag web

# Search by name
cortiva skill search web-search
```

## Installing and Uninstalling

Install a skill on an agent:

```bash
cortiva skill install dev-cortiva web-search
```

This copies the skill's tool definitions and prompt references into the agent's identity directory (`identity/skills.md` is updated to include the new skill).

Uninstall:

```bash
cortiva skill uninstall dev-cortiva web-search
```

This removes the skill's tools and prompts from the agent. The skill itself remains in the registry for other agents.

List skills installed on an agent:

```bash
cortiva skill installed dev-cortiva
```

## The Registry Format

A skill registry is either a local directory or a remote endpoint that serves skill metadata.

### Local Registry

A directory where each subdirectory is a skill:

```
skills/
  web-search/
    skill.yaml
    tools.json
    ...
  code-review/
    skill.yaml
    tools.json
    ...
```

The CLI scans `skill.yaml` files to build the searchable index.

### Registry Index

Each registry can include an optional `index.yaml` for faster lookups:

```yaml
skills:
  - name: web-search
    version: 1.0.0
    description: Search the web using configurable search providers.
    tags: [search, web, research]
  - name: code-review
    version: 1.2.0
    description: Automated code review with configurable rules.
    tags: [code, review, quality]
```

Without an index, the CLI reads each `skill.yaml` individually.

## MCP Integration

Skills use the Model Context Protocol (MCP) format for tool definitions. This means skills are compatible with any MCP-aware agent framework.

A `tools.json` file contains an array of MCP tool definitions:

```json
[
  {
    "name": "web_search",
    "description": "Search the web for information.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "The search query."
        },
        "max_results": {
          "type": "integer",
          "description": "Maximum results to return.",
          "default": 5
        }
      },
      "required": ["query"]
    }
  }
]
```

When the conscious layer receives tools from an installed skill, it can invoke them during task execution. The tool invocations are routed through the terminal adapter or handled directly by the skill's implementation.

## Creating Custom Skills

To create a new skill:

1. Create a directory under `skills/`:

```bash
mkdir -p skills/my-skill/prompts
```

2. Write a `skill.yaml`:

```yaml
name: my-skill
version: 0.1.0
description: What this skill does.
author: your-name
tags:
  - custom

requires:
  env: []
  adapters: []

tools:
  - tools.json

prompts:
  - prompts/main.md
```

3. Define tools in `tools.json` using the MCP format (see above).

4. Write prompt templates in `prompts/`. These are markdown files that get injected into the agent's context when the skill is active. Use them to provide instructions, examples, or domain knowledge.

5. Test by installing on an agent:

```bash
cortiva skill install dev-cortiva my-skill
cortiva agent wake dev-cortiva
```

Check the agent's journal for evidence that the skill's tools and prompts are being used during task execution.

## Skill Configuration

Skills can accept per-agent configuration. When installing a skill, you can pass config values:

```bash
cortiva skill install dev-cortiva web-search --config search_provider=google --config max_results=10
```

These values are stored in the agent's workspace and injected into the skill's tool invocations at runtime.

## CLI Reference

```
cortiva skill list                                    List all skills in registries
cortiva skill search <query>                          Search skills by name or description
cortiva skill search --tag <tag>                      Search skills by tag
cortiva skill install <agent-id> <skill-name>         Install a skill on an agent
cortiva skill uninstall <agent-id> <skill-name>       Uninstall a skill from an agent
cortiva skill installed <agent-id>                    List skills installed on an agent
cortiva skill info <skill-name>                       Show skill details
```
