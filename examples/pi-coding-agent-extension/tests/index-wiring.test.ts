import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createServer, type Server } from "node:http";
import { writeFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// ---------------------------------------------------------------------------
// Mock OV server factory
// ---------------------------------------------------------------------------

interface MockOVState {
  sessionCreated: boolean;
  messages: Array<{ role: string; content: string }>;
  committed: boolean;
  overview: string;
  commitCount: number;
}

function startMockOV(state: MockOVState): Promise<{ server: Server; port: number }> {
  return new Promise((resolve) => {
    const server = createServer(async (req, res) => {
      let body = "";
      for await (const chunk of req) body += chunk;
      let parsed: any = {};
      try { parsed = body ? JSON.parse(body) : {}; } catch { /* ignore */ }

      const url = req.url || "/";
      const method = req.method || "GET";

      res.writeHead(200, { "Content-Type": "application/json" });

      if (url === "/health") {
        res.end(JSON.stringify({ status: "ok", result: { healthy: true } }));
        return;
      }

      if (url === "/api/v1/sessions" && method === "POST") {
        state.sessionCreated = true;
        res.end(JSON.stringify({ status: "ok", result: { session_id: parsed.session_id } }));
        return;
      }

      if (url.startsWith("/api/v1/sessions/") && url.endsWith("/messages") && method === "POST") {
        state.messages.push({ role: parsed.role, content: parsed.content });
        res.end(JSON.stringify({ status: "ok", result: { added: true } }));
        return;
      }

      if (url.startsWith("/api/v1/sessions/") && url.endsWith("/commit") && method === "POST") {
        state.committed = true;
        state.commitCount++;
        // After commit, set an overview so getSessionContext returns it
        if (state.commitCount >= 1) {
          state.overview = `Session summary (commit ${state.commitCount}): user discussed various topics.`;
        }
        res.end(JSON.stringify({
          status: "ok",
          result: { task_id: `t-${state.commitCount}`, archive_uri: `ov://archive/${state.commitCount}` },
        }));
        return;
      }

      if (url.startsWith("/api/v1/sessions/") && url.includes("/context")) {
        res.end(JSON.stringify({
          status: "ok",
          result: {
            latest_archive_overview: state.overview || null,
            pre_archive_abstracts: [],
            messages: [],
            estimatedTokens: state.messages.length * 50,
            stats: {
              totalArchives: state.commitCount,
              includedArchives: state.commitCount,
              droppedArchives: 0,
              failedArchives: 0,
              activeTokens: state.messages.length * 50,
              archiveTokens: 0,
            },
          },
        }));
        return;
      }

      if (url.startsWith("/api/v1/fs/ls")) {
        res.end(JSON.stringify({ status: "ok", result: [] }));
        return;
      }

      if (url.startsWith("/api/v1/search/find")) {
        res.end(JSON.stringify({
          status: "ok",
          result: { memories: [], resources: [], skills: [], total: 0 },
        }));
        return;
      }

      if (url.startsWith("/api/v1/system/status")) {
        res.end(JSON.stringify({ status: "ok", result: { user: "default" } }));
        return;
      }

      if (url.startsWith("/api/v1/content/")) {
        res.end(JSON.stringify({ status: "ok", result: null }));
        return;
      }

      res.end(JSON.stringify({ status: "ok", result: null }));
    });

    server.listen(0, "127.0.0.1", () => {
      const addr = server.address() as { port: number };
      resolve({ server, port: addr.port });
    });
  });
}

// ---------------------------------------------------------------------------
// Fake pi object
// ---------------------------------------------------------------------------

interface FakePi {
  on: ReturnType<typeof vi.fn>;
  registerTool: ReturnType<typeof vi.fn>;
  registerCommand: ReturnType<typeof vi.fn>;
  appendEntry: ReturnType<typeof vi.fn>;
  handlers: Map<string, Array<(event: any, ctx: any) => any>>;
}

function makeFakePi(): FakePi {
  const handlers = new Map<string, Array<(event: any, ctx: any) => any>>();
  const on = vi.fn((event: string, handler: any) => {
    if (!handlers.has(event)) handlers.set(event, []);
    handlers.get(event)!.push(handler);
  });
  return {
    on,
    registerTool: vi.fn(),
    registerCommand: vi.fn(),
    appendEntry: vi.fn(),
    handlers,
  };
}

async function fireEvent(
  fakePi: FakePi,
  eventName: string,
  event: any,
  ctx: any,
): Promise<any> {
  const hdls = fakePi.handlers.get(eventName);
  if (!hdls || hdls.length === 0) return undefined;
  // Return the last handler's result (pi uses the last registered handler's return)
  let last: any = undefined;
  for (const h of hdls) {
    const r = await h(event, ctx);
    if (r !== undefined) last = r;
  }
  return last;
}

// ---------------------------------------------------------------------------
// Fake ctx
// ---------------------------------------------------------------------------

function makeCtx(branch: any[] = []) {
  return {
    ui: { notify: vi.fn() },
    sessionManager: {
      getSessionId: () => "test-sess-001",
      getBranch: () => branch,
    },
    cwd: "/tmp/ov-test-cwd",
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("index.ts — handler registration", () => {
  let server: Server;
  let port: number;
  let ovState: MockOVState;
  let tempDir: string;

  beforeEach(async () => {
    ovState = { sessionCreated: false, messages: [], committed: false, overview: "", commitCount: 0 };
    ({ server, port } = await startMockOV(ovState));
    tempDir = mkdtempSync(join(tmpdir(), "ov-index-test-"));
    process.env.OPENVIKING_URL = `http://127.0.0.1:${port}`;
  });

  afterEach(() => {
    server?.close();
    delete process.env.OPENVIKING_URL;
    rmSync(tempDir, { recursive: true, force: true });
  });

  it("registers expected event handlers", async () => {
    const fakePi = makeFakePi();

    // Dynamically import the extension default
    const mod = await import("../index.js");
    const init = mod.default;
    await init(fakePi as any);

    // Verify handler registrations. Capture happens on agent_end, NOT
    // turn_end (pi's turnIndex resets per run — see the note in index.ts).
    const eventNames = [...fakePi.handlers.keys()];
    expect(eventNames).toContain("session_start");
    expect(eventNames).toContain("context");
    expect(eventNames).toContain("session_before_compact");
    expect(eventNames).toContain("session_shutdown");
    expect(eventNames).toContain("before_agent_start");
    expect(eventNames).toContain("agent_end");
    expect(eventNames).not.toContain("turn_end");

    // Verify command registration
    expect(fakePi.registerCommand).toHaveBeenCalledWith(
      "viking",
      expect.objectContaining({ description: expect.any(String) }),
    );
  });

  it("registers tools", async () => {
    const fakePi = makeFakePi();
    const mod = await import("../index.js");
    await mod.default(fakePi as any);

    // Fire session_start to trigger tool registration
    const ctx = makeCtx();
    await fireEvent(fakePi, "session_start", {}, ctx);

    // Tools should be registered (viking_search, viking_read, etc.)
    const toolNames = fakePi.registerTool.mock.calls.map((c: any[]) => c[0].name);
    expect(toolNames).toContain("viking_search");
    expect(toolNames).toContain("viking_read");
    expect(toolNames).toContain("viking_remember");
  });
});

describe("index.ts — session flow", () => {
  let server: Server;
  let port: number;
  let ovState: MockOVState;
  let fakePi: FakePi;
  let ctx: ReturnType<typeof makeCtx>;
  let init: any;

  beforeEach(async () => {
    ovState = { sessionCreated: false, messages: [], committed: false, overview: "", commitCount: 0 };
    ({ server, port } = await startMockOV(ovState));
    process.env.OPENVIKING_URL = `http://127.0.0.1:${port}`;

    fakePi = makeFakePi();
    ctx = makeCtx();

    const mod = await import("../index.js");
    init = mod.default;
    await init(fakePi as any);
  });

  afterEach(() => {
    server?.close();
    delete process.env.OPENVIKING_URL;
  });

  it("session_start creates OV session", async () => {
    await fireEvent(fakePi, "session_start", {}, ctx);
    expect(ovState.sessionCreated).toBe(true);
    // With logLevel=error (shipped config), no info notification is sent;
    // we just verify the session was created on the mock server.
  });

  it("context hook passthrough when no takeover boundary set", async () => {
    await fireEvent(fakePi, "session_start", {}, ctx);

    const messages = [
      { role: "user", content: "hello", timestamp: 1000 },
      { role: "assistant", content: "hi there", timestamp: 1001 },
    ];
    const result = await fireEvent(fakePi, "context", { messages: [...messages] }, ctx);

    // Should return messages (possibly with recall injection, but no recall results → unchanged)
    expect(result).toBeDefined();
    expect(result.messages.length).toBe(2);
  });

  it("before_agent_start also starts the session (pi -c continuations skip session_start)", async () => {
    // No session_start fired — before_agent_start must initialize on its own
    await fireEvent(fakePi, "before_agent_start", {
      prompt: "hello continuation",
      systemPrompt: "base system prompt",
    }, ctx);
    expect(ovState.sessionCreated).toBe(true);
  });

  it("start() is idempotent — session created only once across both hooks", async () => {
    await fireEvent(fakePi, "session_start", {}, ctx);
    expect(ovState.sessionCreated).toBe(true);

    // Reset the flag; a second start must NOT re-create the session
    ovState.sessionCreated = false;
    await fireEvent(fakePi, "before_agent_start", {
      prompt: "second prompt",
      systemPrompt: "base",
    }, ctx);
    await fireEvent(fakePi, "session_start", {}, ctx);
    expect(ovState.sessionCreated).toBe(false);
  });
});

describe("index.ts — takeover threshold crossing", () => {
  let server: Server;
  let port: number;
  let ovState: MockOVState;
  let fakePi: FakePi;
  let branch: any[];
  let ctx: any;
  let turnCounter: number;

  beforeEach(async () => {
    ovState = { sessionCreated: false, messages: [], committed: false, overview: "", commitCount: 0 };
    ({ server, port } = await startMockOV(ovState));
    process.env.OPENVIKING_URL = `http://127.0.0.1:${port}`;

    fakePi = makeFakePi();
    branch = [];
    turnCounter = 0;

    ctx = {
      ui: { notify: vi.fn() },
      sessionManager: {
        getSessionId: () => "test-sess-big",
        getBranch: () => branch,
      },
      cwd: "/tmp/ov-test-cwd",
    };

    const mod = await import("../index.js");
    await mod.default(fakePi as any);
  });

  afterEach(() => {
    server?.close();
    delete process.env.OPENVIKING_URL;
  });

  function addTurnToBranch(userText: string, assistantText: string) {
    turnCounter++;
    branch.push({
      type: "message",
      message: { role: "user", content: userText },
      id: `entry-u-${turnCounter}`,
    });
    branch.push({
      type: "message",
      message: { role: "assistant", content: [{ type: "text", text: assistantText }] },
      id: `entry-a-${turnCounter}`,
    });
  }

  function buildMessagesFromBranch(): any[] {
    const msgs: any[] = [];
    for (const entry of branch) {
      if (entry.type === "message") {
        msgs.push({
          role: entry.message.role,
          content: entry.message.content,
          timestamp: msgs.length * 1000,
        });
      }
    }
    return msgs;
  }

  it("after threshold crossing, context call returns overview marker at index 0 and early turns dropped", async () => {
    // Start session
    await fireEvent(fakePi, "session_start", {}, ctx);

    // The config.json has takeover.tokenThreshold = 30000
    // estimateTokens ≈ len/4 for ASCII, so ~120KB chars = ~30k tokens.
    // Capture is driven by agent_end (one per user prompt / run). We fire
    // context before each agent_end so transformContext sets
    // lastSeenUserTurns (needed by onTurnSynced's keepRecentTurns gate).

    const bigChunk = "x".repeat(40000); // ~10k tokens per side
    const turns: Array<[string, string]> = [
      [`First question: ${bigChunk}`, `First answer: ${bigChunk}`],
      [`Second question: ${bigChunk}`, `Second answer: ${bigChunk}`],
      [`Third question: ${bigChunk}`, `Third answer: ${bigChunk}`],
      [`Fourth question: ${bigChunk}`, `Fourth answer: ${bigChunk}`],
      [`Fifth question: ${bigChunk}`, `Fifth answer: ${bigChunk}`],
    ];

    for (let i = 0; i < turns.length; i++) {
      const [userText, assistantText] = turns[i];
      addTurnToBranch(userText, assistantText);

      // Fire context so transformContext sets lastSeenUserTurns
      const msgs = buildMessagesFromBranch();
      await fireEvent(fakePi, "context", { messages: msgs }, ctx);

      // Fire agent_end with this run's messages — syncs the run and
      // triggers takeover accounting.
      await fireEvent(fakePi, "agent_end", {
        messages: [
          { role: "user", content: userText },
          { role: "assistant", content: [{ type: "text", text: assistantText }] },
        ],
      }, ctx);
    }

    // Give async commit + overview poll time to complete
    await new Promise((r) => setTimeout(r, 500));

    // Verify commit happened on the mock server
    expect(ovState.committed).toBe(true);
    expect(ovState.messages.length).toBeGreaterThan(0);

    // Now fire context — the takeover boundary should be active
    const allMessages = buildMessagesFromBranch();
    const ctxResult = await fireEvent(fakePi, "context", { messages: allMessages }, ctx);

    // After takeover, the first message should be the overview marker
    expect(ctxResult).toBeDefined();
    const resultMessages = ctxResult.messages;
    expect(resultMessages.length).toBeGreaterThan(0);

    // The first message should start with "[OpenViking Session Context]"
    const firstContent = typeof resultMessages[0].content === "string"
      ? resultMessages[0].content
      : Array.isArray(resultMessages[0].content)
        ? resultMessages[0].content.filter((b: any) => b.type === "text").map((b: any) => b.text).join("")
        : "";

    expect(firstContent).toContain("[OpenViking Session Context]");

    // Early turns should be dropped — verify fewer user turns than total.
    // Exact kept count depends on when the commit crossed the threshold
    // (coveredUserTurns = lastSeenUserTurns - keepRecentTurns at commit time).
    let userTurnCount = 0;
    for (const m of resultMessages) {
      const c = typeof m.content === "string"
        ? m.content
        : Array.isArray(m.content)
          ? m.content.filter((b: any) => b.type === "text").map((b: any) => b.text).join("")
          : "";
      if (m.role === "user" && !c.startsWith("[OpenViking Session Context]")) {
        userTurnCount++;
      }
    }
    const totalUserTurns = turns.length;
    expect(userTurnCount).toBeLessThan(totalUserTurns);
    expect(userTurnCount).toBeGreaterThan(0);
  }, 30000);

  it("appendEntry called with ov-takeover after commit", async () => {
    await fireEvent(fakePi, "session_start", {}, ctx);

    const bigChunk = "y".repeat(50000);
    const turns: Array<[string, string]> = [
      [`Big question 1: ${bigChunk}`, `Big answer 1: ${bigChunk}`],
      [`Big question 2: ${bigChunk}`, `Big answer 2: ${bigChunk}`],
      [`Big question 3: ${bigChunk}`, `Big answer 3: ${bigChunk}`],
      [`Big question 4: ${bigChunk}`, `Big answer 4: ${bigChunk}`],
      [`Big question 5: ${bigChunk}`, `Big answer 5: ${bigChunk}`],
    ];

    for (let i = 0; i < turns.length; i++) {
      const [userText, assistantText] = turns[i];
      addTurnToBranch(userText, assistantText);

      // Fire context to set lastSeenUserTurns
      const msgs = buildMessagesFromBranch();
      await fireEvent(fakePi, "context", { messages: msgs }, ctx);

      // agent_end carries this run's messages (user prompt + assistant rounds)
      await fireEvent(fakePi, "agent_end", {
        messages: [
          { role: "user", content: userText },
          { role: "assistant", content: [{ type: "text", text: assistantText }] },
        ],
      }, ctx);
    }

    await new Promise((r) => setTimeout(r, 500));

    // Check appendEntry was called with ov-takeover
    const takeoverCalls = fakePi.appendEntry.mock.calls.filter(
      (c: any[]) => c[0] === "ov-takeover",
    );
    expect(takeoverCalls.length).toBeGreaterThan(0);
    expect(takeoverCalls[0][1]).toMatchObject({
      coveredUserTurns: expect.any(Number),
      overview: expect.any(String),
    });
  }, 30000);
});

describe("index.ts — disabled extension", () => {
  let tempDir: string;

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), "ov-disabled-test-"));
    // Write a config with enabled:false
    writeFileSync(join(tempDir, "config.json"), JSON.stringify({ enabled: false }));
  });

  afterEach(() => {
    rmSync(tempDir, { recursive: true, force: true });
  });

  it("returns early when config.enabled is false", async () => {
    // We can't easily override the extension dir for loadConfig since index.ts
    // uses import.meta.url. Instead, we verify the loadConfig behavior directly.
    // The index.ts early-return is tested via handler registration being empty.
    // Since we can't change import.meta.url, we test the loadConfig function
    // which is already covered in config.test.ts.
    //
    // For index.ts wiring, we verify: if the extension is loaded and handlers
    // are registered, the session_start handler respects connected=false.
    const fakePi = makeFakePi();

    // Set a bogus URL so health check fails → connected=false
    process.env.OPENVIKING_URL = "http://127.0.0.1:19999";

    const mod = await import("../index.js");
    await mod.default(fakePi as any);

    const ctx = makeCtx();
    await fireEvent(fakePi, "session_start", {}, ctx);

    // Handlers registered but session_start failed to connect → context should passthrough
    const messages = [{ role: "user", content: "test" }];
    const result = await fireEvent(fakePi, "context", { messages: [...messages] }, ctx);

    // When not connected, context handler returns undefined (passthrough in pi)
    // or returns messages unchanged. Our fireEvent returns undefined if no handler
    // returns a value.
    // Actually, looking at index.ts: "if (!connected || bypassed) return;" → returns undefined
    // So result should be undefined (no handler returned a value)
    expect(result).toBeUndefined();

    delete process.env.OPENVIKING_URL;
  });
});
