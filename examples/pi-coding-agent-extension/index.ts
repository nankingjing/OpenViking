/**
 * Pi OpenViking Extension
 *
 * Integrates pi with an OpenViking context database for persistent,
 * cross-session memory. Syncs conversation turns to OV, recalls
 * relevant memories on each prompt, and commits sessions for long-term
 * memory extraction.
 *
 * Design informed by: OpenClaw (synchronous recall), Claude Code plugin
 * (most mature, production-hardened), Hermes (anti-pattern: stale prefetch).
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { dirname } from "node:path";
import { readFileSync, existsSync } from "node:fs";
import { loadConfig, type OVConfig } from "./config.js";
import { OVClient } from "./client.js";
import { RecallManager } from "./recall.js";
import { SyncManager, estimateTokens } from "./sync.js";
import { IndexBuilder } from "./index-builder.js";
import { registerTools } from "./tools.js";
import { TakeoverManager } from "./takeover.js";
import { appendFileSync } from "node:fs";

export default async function (pi: ExtensionAPI) {
  // --- Load config ---
  const config = loadConfig(dirname(new URL(import.meta.url).pathname));
  if (!config.enabled) return;

  // Env overrides
  if (process.env.OPENVIKING_URL) config.endpoint = process.env.OPENVIKING_URL;
  if (process.env.OPENVIKING_API_KEY) config.apiKey = process.env.OPENVIKING_API_KEY;
  if (process.env.OPENVIKING_ACCOUNT) config.account = process.env.OPENVIKING_ACCOUNT;
  if (process.env.OPENVIKING_USER) config.user = process.env.OPENVIKING_USER;
  if (process.env.OPENVIKING_AGENT_ID) config.agentId = process.env.OPENVIKING_AGENT_ID;

  // --- Initialize modules ---
  const client = new OVClient(config);
  const recall = new RecallManager(client, config);
  const sync = new SyncManager(client, config);
  const indexBuilder = new IndexBuilder(client, config);
  const debugLog = (msg: string) => {
    // Opt-in file logger (TUI-safe): OV_DEBUG_LOG=/path/to/file
    const path = process.env.OV_DEBUG_LOG;
    if (!path) return;
    try { appendFileSync(path, `${new Date().toISOString()} ${msg}\n`); } catch { /* best effort */ }
  };
  const takeover = new TakeoverManager({
    client,
    sync,
    config,
    appendEntry: (customType, data) => pi.appendEntry(customType, data),
    log: debugLog,
  });

  // Session state
  let connected = false;
  let bypassed = false;
  let profileBlock = "";
  let archiveOverview = "";
  let toolsRegistered = false;

  // ================================================================
  // Event Handlers
  // ================================================================

  // Idempotent startup: session_start doesn't fire for `pi -c` continuations,
  // so both session_start and before_agent_start funnel through here.
  let started = false;
  const start = async (ctx: { ui: { notify: (msg: string, level: any) => void }; sessionManager: { getSessionId(): string; getBranch(): unknown[] } }) => {
    if (started) return;
    started = true;
    debugLog(`start: begin (session ${ctx.sessionManager.getSessionId()}, entries ${ctx.sessionManager.getBranch().length})`);

    // Bypass check
    const cwd = process.cwd();
    for (const pattern of config.bypassPatterns) {
      if (matchBypass(cwd, pattern)) {
        bypassed = true;
        return;
      }
    }

    // Health check
    connected = await client.health();
    debugLog(`start: health connected=${connected}`);
    if (!connected) {
      if (config.logLevel === "info") {
        ctx.ui.notify("OpenViking: server not reachable", "warning");
      }
      return;
    }

    // Ensure OV session
    const piSessionId = ctx.sessionManager.getSessionId();
    const ok = await sync.ensureSession(piSessionId);
    if (!ok) {
      connected = false;
      if (config.logLevel !== "silent") {
        ctx.ui.notify("OpenViking: failed to create session", "error");
      }
      return;
    }

    // Profile injection
    profileBlock = await buildProfileBlock(client, config);

    // Takeover: restore boundary/overview persisted in session entries.
    // The context hook then re-injects the overview inline, so the
    // system-prompt rehydration below is only for non-takeover mode.
    if (config.takeoverEnabled) {
      takeover.restore(ctx.sessionManager.getBranch() as any);
    } else if (sync.sessionId) {
      // Resume rehydration — fetch archive overview if session was previously committed
      archiveOverview = await fetchArchiveOverview(client, sync.sessionId, config);
    }

    // Build memory index
    await indexBuilder.buildIndex();

    // Register tools
    if (!toolsRegistered) {
      registerTools(pi, client, sync);
      toolsRegistered = true;
    }

    if (config.logLevel === "info") {
      ctx.ui.notify(`OpenViking connected (${piSessionId.slice(0, 8)}...)`, "info");
    }
  };

  // --- session_start ---
  pi.on("session_start", async (_event, ctx) => {
    await start(ctx);
  });

  // --- before_agent_start ---
  pi.on("before_agent_start", async (event, ctx) => {
    // session_start doesn't fire for pi -c continuations — start here instead
    await start(ctx);

    if (!connected || bypassed) return;

    // Synchronous recall
    await recall.searchAndCache(event.prompt);

    // Compose system prompt additions
    const parts: string[] = [];
    if (profileBlock) parts.push(profileBlock);
    if (archiveOverview) parts.push(archiveOverview);

    const idx = indexBuilder.getIndex();
    if (idx) parts.push(idx);

    const additions = parts.join("\n\n");
    if (!additions) return;

    return {
      systemPrompt: event.systemPrompt + "\n\n" + additions,
    };
  });

  // --- context ---
  // Order matters: takeover first (replace committed history with the OV
  // archive overview), then recall injection into the (kept) last user message.
  pi.on("context", async (event, _ctx) => {
    if (!connected || bypassed) return;
    const afterTakeover = takeover.transformContext(event.messages as any) as any[];
    const messages = recall.injectRecall(afterTakeover);
    return { messages };
  });

  // --- agent_end: capture the whole run ---
  // NOTE: capture happens on agent_end, not turn_end. Pi's turnIndex counts
  // LLM rounds *within* one agent run and resets to 0 on every prompt, so a
  // turn_end-based dedup counter (`turnIndex <= synced`) silently skipped the
  // first round of every run — in one-shot `pi -p` usage nothing was ever
  // synced. agent_end fires once per user prompt with exactly this run's new
  // messages (user prompt + assistant rounds + tool results).
  let runCounter = 0;
  pi.on("agent_end", async (event, _ctx) => {
    recall.invalidate();
    if (!connected || bypassed || !config.syncTurns) return;

    const msgs = (event.messages ?? []) as any[];

    // User text: ALL user messages of this run (prompt + steering/follow-up
    // messages injected mid-run). In faithful/takeover mode dropping steering
    // input would silently lose it from the archive overview.
    const userTexts: string[] = [];
    for (const m of msgs) {
      if (m?.role !== "user") continue;
      const text = typeof m.content === "string"
        ? m.content
        : Array.isArray(m.content)
          ? m.content
              .filter((b: any) => b.type === "text")
              .map((b: any) => b.text)
              .join("")
          : "";
      if (text.trim()) userTexts.push(text);
    }
    const userText = userTexts.join("\n\n");

    // Assistant text + tool lines across all assistant rounds of the run
    let assistantText = "";
    const toolLines: string[] = [];
    const toolNames: string[] = [];
    for (const m of msgs) {
      if (m?.role !== "assistant" || !Array.isArray(m.content)) continue;
      for (const block of m.content) {
        if (block.type === "text") {
          assistantText += block.text + "\n";
        } else if (block.type === "toolCall") {
          toolNames.push(block.name);
          toolLines.push(
            `[tool: ${block.name}]\n${JSON.stringify(block.arguments)}`,
          );
        }
      }
    }
    if (toolNames.length > 0) {
      assistantText = `[assistant used tools: ${toolNames.join(", ")}]\n` + assistantText;
    }

    const turnTokens = await sync.syncTurn(
      userText, assistantText, toolLines, ++runCounter,
    );
    debugLog(`agent_end: run ${runCounter} synced ~${turnTokens} tokens (state ${JSON.stringify(takeover.state)})`);
    // Takeover accounting: commit + advance boundary at the token threshold.
    await takeover.onTurnSynced(turnTokens);
  });

  // --- session_before_compact ---
  pi.on("session_before_compact", async (event, _ctx) => {
    if (!connected || bypassed) return;

    if (config.takeoverEnabled) {
      // Takeover: OV owns the summary. Commit, then hand pi a compaction
      // whose summary is OV's archive overview at pi's own (tool-safe) cut
      // point. Fail-open: undefined → pi's default compaction proceeds.
      const prep = (event as any).preparation ?? {};
      return await takeover.handleBeforeCompact({
        firstKeptEntryId: prep.firstKeptEntryId,
        tokensBefore: prep.tokensBefore ?? 0,
      });
    }

    // Non-takeover mode: flush + commit, cache overview for rehydration,
    // let pi's default compaction run.
    await sync.shutdown();
    const archiveId = await sync.commit(true);
    if (archiveId && sync.sessionId) {
      archiveOverview = await fetchArchiveOverview(
        client, sync.sessionId, config,
      );
    }
  });

  // --- session_shutdown ---
  pi.on("session_shutdown", async (_event, ctx) => {
    if (!connected || bypassed) return;

    await sync.shutdown();

    // Mirror MEMORY.md
    if (config.mirrorMemoryWrites && sync.sessionId) {
      const memoryPath = `${ctx.cwd}/.memory/MEMORY.md`;
      if (existsSync(memoryPath)) {
        try {
          const content = readFileSync(memoryPath, "utf8");
          if (content.trim()) {
            await client.addMessage(
              sync.sessionId, "user",
              `[Memory mirror]\n${content.slice(0, 50000)}`,
            );
          }
        } catch {
          // Best effort
        }
      }
    }

    // Final commit
    if (config.commitOnShutdown) {
      await sync.commit(true);
    }
    await takeover.shutdown();
  });

  // ================================================================
  // Commands
  // ================================================================

  pi.registerCommand("viking", {
    description: "OpenViking status and manual operations. Use 'commit' to force a sync.",
    handler: async (args, ctx) => {
      if (!connected) {
        ctx.ui.notify("OpenViking: not connected", "warning");
        return;
      }

      if (args?.trim() === "commit") {
        const ok = config.takeoverEnabled
          ? await takeover.commitAndAdvance()
          : (await sync.shutdown(), (await sync.commit(true)) !== null);
        if (ok) {
          await indexBuilder.buildIndex();
          ctx.ui.notify("OpenViking: committed successfully", "info");
        } else {
          ctx.ui.notify(
            "OpenViking: boundary not advanced (commit failed, busy, or overview still extracting — retry shortly)",
            "error",
          );
        }
        return;
      }

      // Status
      const sid = sync.sessionId ?? "none";
      const t = takeover.state;
      const takeoverInfo = config.takeoverEnabled
        ? ` | takeover: ${t.coveredUserTurns}/${t.lastSeenUserTurns} turns archived, ~${t.pendingTokens} tokens pending`
        : "";
      ctx.ui.notify(
        `OpenViking: ${connected ? "connected" : "disconnected"} | session: ${sid.slice(0, 12)}...${takeoverInfo}`,
        "info",
      );
    },
  });
}

// ================================================================
// Helper Functions
// ================================================================

/** Simple bypass pattern matching (prefix and glob). */
function matchBypass(cwd: string, pattern: string): boolean {
  if (pattern.startsWith("*")) {
    return cwd.endsWith(pattern.slice(1));
  }
  if (pattern.endsWith("*")) {
    return cwd.startsWith(pattern.slice(0, -1));
  }
  return cwd === pattern || cwd.startsWith(pattern + "/");
}

/** Build the <openviking-context> profile block. */
async function buildProfileBlock(
  client: OVClient, config: OVConfig,
): Promise<string> {
  try {
    const memUri = await client.resolveTargetUri("viking://user/memories");
    const entries = await client.ls(memUri);

    // Look for profile.md
    const profileEntry = entries.find(e => e.name === "profile.md");
    let profileText = "";
    if (profileEntry) {
      const content = await client.readContent(`${memUri}/profile.md`);
      if (content) {
        // Profile elision: keep head (8 lines) + tail (fits budget)
        const lines = content.split("\n");
        if (lines.length > 20) {
          const head = lines.slice(0, 8).join("\n");
          const tailBudget = config.profileBudget - 200;
          const tail = lines.slice(-Math.max(10, tailBudget)).join("\n");
          profileText = head + "\n...\n" + tail;
        } else {
          profileText = content;
        }
      }
    }

    // List preferences and entities directories
    const prefUri = `${memUri}/preferences`;
    const entUri = `${memUri}/entities`;
    const [prefs, ents] = await Promise.all([
      client.ls(prefUri),
      client.ls(entUri),
    ]);

    const sections: string[] = ["<openviking-context>"];
    if (profileText) {
      sections.push(`<user-profile>${profileText}</user-profile>`);
    }
    if (prefs.length > 0 || ents.length > 0) {
      sections.push("<available-memories>");
      if (prefs.length > 0) {
        sections.push(`  ${prefUri}/ (${prefs.length} entries)`);
      }
      if (ents.length > 0) {
        sections.push(`  ${entUri}/ (${ents.length} entries)`);
      }
      sections.push("</available-memories>");
    }
    sections.push("</openviking-context>");

    const block = sections.join("\n");
    // Budget check
    const tokens = estimateTokens(block);
    if (tokens > config.profileBudget) {
      return block.slice(0, config.profileBudget * 3);
    }
    return block;
  } catch {
    return "";
  }
}

/** Fetch archive overview for rehydration using the session context API. */
async function fetchArchiveOverview(
  client: OVClient, sessionId: string, config: OVConfig,
): Promise<string> {
  try {
    const ctx = await client.getSessionContext(sessionId, config.resumeContextBudget);
    if (!ctx || !ctx.latest_archive_overview) return "";

    const result = `[Session History Summary]\n${ctx.latest_archive_overview}`;
    const tokens = estimateTokens(result);
    if (tokens > config.resumeContextBudget) {
      return result.slice(0, config.resumeContextBudget * 3);
    }
    return result;
  } catch {
    return "";
  }
}
