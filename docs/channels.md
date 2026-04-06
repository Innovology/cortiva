# Channel Adapters

Channels are how Cortiva agents communicate -- with each other and with humans. Every message flows through a channel adapter, whether it is a Slack workspace, a Discord server, a Microsoft Teams channel, or an in-process queue.

The channel system is pull-based. The fabric's heartbeat drives message retrieval: on each tick, agents call `receive()` to check for new messages. This avoids the need for HTTP servers, webhook endpoints, or persistent socket connections inside the framework.

## The ChannelAdapter Protocol

All channel adapters implement the same three-method protocol defined in `cortiva.adapters.protocols`:

```python
class ChannelAdapter(Protocol):
    async def send(
        self,
        sender: str,
        recipient: str,
        content: str,
        *,
        channel: str | None = None,
        thread_id: str | None = None,
    ) -> Message:
        """Send a message from one agent to another or to a channel."""
        ...

    async def receive(
        self,
        agent_id: str,
        *,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[Message]:
        """Check for messages addressed to this agent."""
        ...

    async def listen(
        self,
        agent_id: str,
        channels: list[str],
    ) -> None:
        """Subscribe to channels. Messages will be queued for the agent
        and available via receive()."""
        ...
```

**`send()`** posts a message. The `sender` and `recipient` are agent IDs (or `"human"`). The optional `channel` parameter targets a specific channel; `thread_id` continues an existing thread.

**`receive()`** polls for new messages addressed to the given agent. The `since` parameter filters out older messages. The `limit` caps the number of messages returned per call.

**`listen()`** subscribes an agent to one or more channels. After subscribing, messages posted to those channels appear in `receive()` results.

All three methods return or consume `Message` dataclass instances:

```python
@dataclass
class Message:
    id: str
    sender: str            # agent_id or "human"
    recipient: str         # agent_id, channel name, or "human"
    content: str
    timestamp: datetime
    thread_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

## Slack Adapter

The Slack adapter uses `slack_sdk.web.async_client.AsyncWebClient` in a polling model. No Bolt framework, no HTTP server -- the fabric heartbeat drives message retrieval.

### Setup

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps).
2. Add the following bot token scopes: `chat:write`, `channels:history`, `channels:read`.
3. Install the app to your workspace and copy the Bot User OAuth Token.
4. Install the SDK: `pip install 'slack-sdk>=3.0'`

### Configuration

```yaml
channel:
  adapter: slack
  config:
    token: "xoxb-your-bot-token"        # or set SLACK_BOT_TOKEN env var
    default_channel: "C0123456789"       # fallback channel ID
```

The token can be omitted from the config file if the `SLACK_BOT_TOKEN` environment variable is set.

### How It Works

- **`send()`** calls `chat_postMessage`. Each message includes Cortiva metadata (sender agent ID, recipient) so other agents can route correctly.
- **`receive()`** calls `conversations_history` on all subscribed channels, tracking the last-seen timestamp per channel to avoid re-processing old messages. Bot messages are automatically filtered out to prevent loops. Messages are routed to agents using metadata, `@mention` patterns, or broadcast.
- **`listen()`** stores channel subscriptions in memory. Pass Slack channel IDs (e.g., `"C0123456789"`).

### Message Routing

The Slack adapter supports three routing modes:

- **Metadata routing** -- Cortiva embeds sender and recipient in Slack message metadata. Messages with a specific recipient are delivered only to that agent.
- **@mention routing** -- If the message text contains `@agent-id`, only the mentioned agents receive it.
- **Broadcast** -- Messages with no specific routing are delivered to all subscribed agents.

## Discord Adapter

The Discord adapter uses `discord.py` with the `message_content` intent. Like Slack, it operates in a polling model driven by the fabric heartbeat.

### Setup

1. Create a Discord application at [discord.com/developers](https://discord.com/developers/applications).
2. Create a bot and enable the **Message Content Intent** under Privileged Gateway Intents.
3. Copy the bot token.
4. Invite the bot to your server with `Send Messages` and `Read Message History` permissions.
5. Install the SDK: `pip install 'discord.py>=2.0'`

### Configuration

The Discord adapter is not yet in the default adapter registry. Register it manually or use it directly:

```python
from cortiva.adapters.channel.discord import DiscordChannelAdapter

adapter = DiscordChannelAdapter(
    token="your-bot-token",          # or set DISCORD_BOT_TOKEN env var
    default_channel=1234567890,      # Discord channel ID (integer)
)
```

To add it to the config registry, add the following entry to `_CHANNEL_ADAPTERS` in `cortiva/core/config.py`:

```python
"discord": ("cortiva.adapters.channel.discord", "DiscordChannelAdapter"),
```

Then use it in `cortiva.yaml`:

```yaml
channel:
  adapter: discord
  config:
    token: "your-bot-token"          # or set DISCORD_BOT_TOKEN env var
    default_channel: 1234567890
```

### How It Works

- **`send()`** sends a message to a Discord channel. Cortiva routing metadata is embedded in a hidden embed footer with the format `cortiva:<sender>:<recipient>`.
- **`receive()`** fetches channel history using `channel.history()`, tracking the last-seen message ID per channel. Own messages and non-Cortiva bot messages are filtered out.
- **`listen()`** stores channel subscriptions. Pass Discord channel IDs as strings (they are converted to integers internally).

### Message Routing

Routing works the same way as Slack: metadata-based, `@mention`-based, or broadcast.

## Teams Adapter

The Microsoft Teams adapter supports two operating modes:

1. **Webhook mode** -- outbound-only posting via Office 365 Incoming Webhook connectors. Simple to set up, but `receive()` always returns an empty list.
2. **Graph API mode** -- full bidirectional messaging via Microsoft Graph. Requires Azure AD app registration.

### Setup: Webhook Mode

1. In Teams, add an Incoming Webhook connector to your target channel.
2. Copy the webhook URL.

```yaml
channel:
  adapter: teams
  config:
    webhook_url: "https://outlook.office.com/webhook/..."
    # or set TEAMS_WEBHOOK_URL env var
```

Webhook mode requires no additional Python packages.

### Setup: Graph API Mode

1. Register an application in Azure AD.
2. Grant `ChannelMessage.Send` and `ChannelMessage.Read.All` application permissions.
3. Create a client secret.
4. Install dependencies: `pip install 'msal>=1.20' 'httpx>=0.24'`

```yaml
channel:
  adapter: teams
  config:
    client_id: "your-client-id"           # or TEAMS_CLIENT_ID env var
    client_secret: "your-client-secret"   # or TEAMS_CLIENT_SECRET env var
    tenant_id: "your-tenant-id"           # or TEAMS_TENANT_ID env var
    default_team_id: "team-guid"
    default_channel_id: "channel-guid"
```

Like Discord, the Teams adapter is not yet in the default config registry. Register it by adding to `_CHANNEL_ADAPTERS` in `cortiva/core/config.py`:

```python
"teams": ("cortiva.adapters.channel.teams", "TeamsChannelAdapter"),
```

### How It Works

- **`send()`** posts via webhook (simple POST) or Graph API (`POST .../messages`). Graph mode supports thread replies.
- **`receive()`** in Graph mode fetches messages from `GET .../messages` and tracks the last-seen message ID. In webhook mode, returns an empty list.
- **`listen()`** stores subscriptions as `(team_id, channel_id)` tuples. Pass channel strings in `"team_id:channel_id"` format.

### Graph API Authentication

The adapter uses MSAL client credentials flow to obtain OAuth2 tokens. Tokens are cached and refreshed automatically. The adapter acquires tokens scoped to `https://graph.microsoft.com/.default`.

## Internal Adapter

The internal adapter routes messages between agents using `asyncio.Queue` instances -- no external services, no network calls, no configuration secrets. It is the default choice for testing, single-process deployments, and local development.

### Configuration

```yaml
channel:
  adapter: internal
  config: {}
```

Like Discord and Teams, the internal adapter is not yet in the default config registry. Register it by adding to `_CHANNEL_ADAPTERS` in `cortiva/core/config.py`:

```python
"internal": ("cortiva.adapters.channel.internal", "InternalChannelAdapter"),
```

Or use it directly:

```python
from cortiva.adapters.channel.internal import InternalChannelAdapter

adapter = InternalChannelAdapter()
```

### How It Works

- **`send()`** creates a `Message` and enqueues it. If a `channel` is specified, the message is broadcast to all subscribers of that channel (excluding the sender). If no channel is given, it is delivered as a direct message to the recipient's queue.
- **`receive()`** drains the agent's queue. Messages older than `since` are discarded. Messages beyond the `limit` are put back for the next call.
- **`listen()`** subscribes an agent to named broadcast channels and lazily creates the agent's queue.

The adapter is thread-safe: queue and subscription mutations are protected by an `asyncio.Lock`.

### No Dependencies

The internal adapter requires nothing beyond the Python standard library. It is ideal for:

- Unit and integration tests
- Single-process agent deployments
- Local development before connecting to Slack or Discord
- Demos and prototyping

## When to Use Which Adapter

| Scenario | Adapter | Why |
|---|---|---|
| Unit tests | Internal | No setup, no dependencies, fast |
| Local development | Internal | Get started immediately |
| Single-process deployment | Internal | No external services needed |
| Team already on Slack | Slack | Agents join existing workspace conversations |
| Team already on Discord | Discord | Agents join existing server channels |
| Enterprise / Microsoft shop | Teams (Graph) | Full bidirectional Teams integration |
| Outbound notifications only | Teams (Webhook) | Simplest Teams setup, no Azure AD required |
| Custom message bus | Write your own | Implement the three-method protocol |

## Common Patterns

### Multi-Agent Broadcast

Subscribe all agents to a shared channel, then broadcast:

```python
# During agent setup
await channel.listen("agent-alice", ["#general"])
await channel.listen("agent-bob", ["#general"])

# Any agent can broadcast
await channel.send("agent-alice", "broadcast", "Status update: task complete", channel="#general")
```

### Direct Messages

Send a message to a specific agent without a channel:

```python
await channel.send("agent-alice", "agent-bob", "Can you review this?")
```

### Thread Replies

Continue a conversation in a thread:

```python
msg = await channel.send("agent-alice", "agent-bob", "Starting review")
await channel.send("agent-bob", "agent-alice", "Looks good", thread_id=msg.thread_id)
```
