# OV Context Takeover — Design

Layer added 2026-07 on top of the existing extension (see DESIGN.md for the base
architecture: recall, capture, commit, tools, profile/index injection). This layer
makes OpenViking the authoritative long-term context store for pi, mirroring
vikingbot's `session_context_enabled` mode, implemented through pi's `context` hook.

Target: `@earendil-works/pi-coding-agent` ^0.80.3 (renamed from `@mariozechner/*`;
all APIs used here are source-compatible with what the base extension already did).

## The vikingbot model, translated to pi

| vikingbot (Python daemon)                          | this extension (pi)                                        |
|----------------------------------------------------|------------------------------------------------------------|
| `append_messages` — incremental sync to OV session | WriteQueue (existing `sync.ts`)                            |
| `commit_session` at `commit_token_threshold`       | `TakeoverManager.maybeCommit()` at `takeoverTokenThreshold`|
| OV `get_session_context` → archive overview        | same endpoint, cached overview                             |
| `session.clear()` after commit                     | **`context` hook drops pre-boundary messages**             |
| history = overview + recent live messages          | history = overview block + post-boundary pi messages       |

Pi's session file is never rewritten. The takeover happens at the LLM-call
boundary: the `context` hook is pi's officially supported "what the model sees"
filter.

## State

```ts
interface TakeoverState {
  coveredUserTurns: number;  // user turns covered by the OV archive (0 = none)
  overview: string;          // cached latest_archive_overview text ("" = none)
  pendingTokens: number;     // synced-token pressure; carried across pi -p/-c processes
  // in-memory only:
  fingerprint: string | null; // of the last covered message; lazily materialized
}
```

The boundary is expressed in **user turns**, not message indexes: at `context`
time the manager finds the first message of the (covered+1)-th user turn and
drops everything before it — user messages are always valid cut points, so a
toolResult can never be orphaned. Persisted via `pi.appendEntry("ov-takeover",
state)` after every commit and at shutdown; restored at startup (both
`session_start` and `before_agent_start`, since `pi -c` continuations skip
session_start) by scanning entries backwards. The fingerprint re-materializes
on the first `context` call; if it later stops matching (branch navigation,
`/tree`, foreign compaction), the boundary resets to 0 — full history until the
next commit re-establishes it. Correctness over cleverness.

## Event flow

### `context` (every LLM call)
1. If boundary > 0 and fingerprint matches: drop `messages[0 .. boundary-1]`,
   prepend a user-role message:
   `[OpenViking Session Context] — earlier conversation, archived and summarized:\n<overview>`
2. Then run existing recall injection into the last user message.
3. **Stability**: between commits the boundary and overview are frozen, so the
   injected prefix is byte-identical call-to-call — prompt cache stays warm.
4. **Tool pairing**: boundary is only ever set at a turn start (see below), so a
   toolResult can never be orphaned.

### turn_end (existing capture + new accounting)
After the existing `sync.syncTurn(...)`, add the turn's estimated tokens to
`pendingTokens`. When `pendingTokens >= takeoverTokenThreshold`:
1. flush WriteQueue, `commitSession(wait: background)` — commit is async server-side;
2. compute the new boundary: walk back from the end keeping `keepRecentTurns`
   turn-starts (a turn start = a user message that begins a turn; never a
   toolResult); everything before that index is covered;
3. fetch `latest_archive_overview` (poll with backoff up to ~30 s; until it
   arrives, keep serving the previous overview + previous boundary — never
   inject an empty overview);
4. persist state entry.

### `session_before_compact` (pi-triggered, should be rare)
Because the context hook keeps effective context small, pi's own threshold
(`contextTokens > contextWindow - reserveTokens`) rarely trips. If it does
(giant recent turns), we do NOT let pi's summarizer run:
1. flush + commit(wait: true-ish, bounded poll);
2. return `{ compaction: { summary: ovOverviewText, firstKeptEntryId:
   preparation.firstKeptEntryId, tokensBefore: preparation.tokensBefore,
   details: { source: "openviking" } } }` — pi records a CompactionEntry whose
   summary is OV's, at pi's own tool-safe cut point;
3. reset takeover boundary to 0 (the CompactionEntry now covers that span).
If OV is unreachable, return nothing → pi's default compaction proceeds
(fail-open; never brick the agent).

### `session_start` (resume)
Restore state from the last `ov-takeover` entry; re-fetch overview if empty.
Existing profile/index/rehydration behavior unchanged.

### `session_shutdown`
Existing: flush + final commit. New: persist final state entry (best effort).

## Capture fidelity in takeover mode

The archive overview is generated from what we synced. The base extension's
`shouldCapture` keyword/noise filter is fine for memory extraction but would
drop turns from the model's effective history. Therefore when takeover is
enabled, capture switches to **faithful mode**: every user/assistant turn is
synced (injected blocks still stripped, tool summary lines kept); only empty
content and slash-commands are skipped. `captureMode` config is ignored in
takeover mode with a log note.

## Config additions (config.json)

```jsonc
{
  "takeover": {
    "enabled": true,
    "tokenThreshold": 30000,   // OV commit threshold (vikingbot: 200k for chat; coding turns are fatter, keep smaller)
    "keepRecentTurns": 3,      // live turns kept after boundary
    "overviewBudget": 3000,    // tokens; overview truncated beyond this
    "overviewPollMs": 2000,    // poll interval for post-commit overview
    "overviewPollMax": 15      // max polls (~30s)
  }
}
```

## Failure modes

| Failure                         | Behavior                                            |
|---------------------------------|-----------------------------------------------------|
| OV down at session start        | connected=false → all hooks no-op (existing guard)  |
| commit accepted, overview slow  | keep previous overview/boundary until it lands      |
| commit fails                    | pendingTokens retained; retry at next threshold hit |
| branch switch invalidates state | fingerprint mismatch → boundary reset, full history |
| pi compaction + OV down         | fall through to pi default compaction               |

## Test plan

Unit (vitest, mocked OV via undici-free `fetch` stub or local `node:http` server):
- boundary computation: turn-start detection, toolResult never first-kept, keepRecentTurns honored
- context hook: drop + overview injection, byte-stability across calls, fingerprint mismatch reset, idempotent recall injection ordering
- threshold commits: pendingTokens accounting, overview polling, state entry persistence shape
- session_before_compact: returns OV summary with preparation's cut point; fail-open path
- capture: faithful mode overrides keyword filter; stripping still applied
- client: new/changed endpoints (`getSessionContext` token budget param, commit)

Live e2e (acceptance gate; scripts/e2e-live.sh, env-gated):
- real pi 0.80.3 headless (`pi --mode json -e ./index.ts`), provider = super-relay
  (`model_api/experimental_0630`, x-session-id header), OV = https://ov.zaynjarvis.com;
- multi-turn conversation crossing a low takeoverTokenThreshold → assert OV session
  has messages, archive created, next turn's provider request contains
  `[OpenViking Session Context]` and NOT the dropped early turns (inspect via
  `before_provider_request` logger or json-mode event stream);
- resume (`pi -c`) restores boundary; recall block appears for a related prompt.
