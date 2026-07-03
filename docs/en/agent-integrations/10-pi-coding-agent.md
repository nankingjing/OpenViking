# Pi Coding Agent Extension

Use the OpenViking extension for [Pi coding agent](https://pi.dev) when you want Pi to use OpenViking as its long-term memory and context backend. The extension runs inside Pi's native TypeScript extension system: no MCP sidecar, no wrapper command, no separate daemon.

Source: [examples/pi-coding-agent-extension](https://github.com/volcengine/OpenViking/tree/main/examples/pi-coding-agent-extension)

## What it does

- **Auto-recall before each model call**: searches OpenViking with the current prompt and injects relevant memories into the same turn.
- **Auto-capture after each run**: syncs user prompts, steering/follow-up messages, assistant text, and tool-use summaries into an OpenViking session.
- **Context takeover by default**: committed Pi history is replaced in the model's view by OpenViking's archive overview plus a few recent live turns.
- **Manual tools**: registers `viking_search`, `viking_read`, `viking_browse`, `viking_remember`, `viking_forget`, `viking_add_resource`, and `viking_archive_expand`.
- **Manual command**: `/viking` shows status and supports manual commit.

Context takeover means OpenViking, not Pi's local compactor, owns long-term context. Pi's session file is not rewritten; the extension filters what the model sees through Pi's `context` hook.

## Install

Prerequisites:

- Pi coding agent `0.80.3` or compatible `0.80.x`
- Node.js 20+
- A reachable OpenViking server

Copy the extension into Pi's global extension directory:

```bash
mkdir -p ~/.pi/agent/extensions
cp -r examples/pi-coding-agent-extension ~/.pi/agent/extensions/openviking
```

Pi auto-discovers `~/.pi/agent/extensions/openviking/index.ts` on next launch.

## Configure

For a local unauthenticated server, the default config works:

```bash
pi
```

For a remote server, set environment variables before starting Pi:

```bash
export OPENVIKING_URL="https://your-openviking.example.com"
export OPENVIKING_API_KEY="<api-key>"
export OPENVIKING_ACCOUNT="my-team"   # optional multi-tenant account
export OPENVIKING_USER="alice"        # optional multi-tenant user
export OPENVIKING_AGENT_ID="pi"       # optional agent identity
pi
```

Or edit `~/.pi/agent/extensions/openviking/config.json`:

```json
{
  "enabled": true,
  "endpoint": "https://your-openviking.example.com",
  "apiKey": "<api-key>",
  "account": "my-team",
  "user": "alice",
  "agentId": "pi"
}
```

Environment variables override `config.json`.

## Context takeover settings

The extension enables takeover by default:

```json
{
  "takeover": {
    "enabled": true,
    "tokenThreshold": 30000,
    "keepRecentTurns": 3,
    "overviewBudget": 3000,
    "overviewPollMs": 2000,
    "overviewPollMax": 15
  }
}
```

When synced-token pressure crosses `tokenThreshold`, the extension flushes queued turns, commits the OpenViking session, waits briefly for the latest archive overview, advances the boundary, and keeps only `keepRecentTurns` live in the model context. If OpenViking is unavailable, takeover fails open and Pi continues normally.

## Verify

Start Pi and look for the OpenViking startup status. Then run:

```text
/viking
```

You can also ask Pi to remember a fact, then verify from another shell:

```bash
ov ls viking://user/default/sessions/
ov find "the fact you asked Pi to remember"
```

## Development

```bash
cd examples/pi-coding-agent-extension
npm install
npm run typecheck
npm test
```

Live acceptance tests drive real Pi, a real OpenViking server, and a real model provider:

```bash
OPENVIKING_URL=https://your-ov \
OPENVIKING_API_KEY=... \
SUPER_RELAY_API_KEY=... \
npm run e2e
```

## See also

- [Extension README](https://github.com/volcengine/OpenViking/blob/main/examples/pi-coding-agent-extension/README.md)
- [Context takeover design](https://github.com/volcengine/OpenViking/blob/main/examples/pi-coding-agent-extension/TAKEOVER.md)
- [Agent integrations overview](./01-overview.md)
- [MCP Clients](./06-mcp-clients.md) for generic tool-only integrations
