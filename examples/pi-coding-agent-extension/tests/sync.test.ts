import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  SyncManager,
  WriteQueue,
  stripInjectedBlocks,
  shouldCapture,
  estimateTokens,
  truncateToTokens,
} from "../sync.js";
import type { OVConfig } from "../config.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function baseConfig(overrides: Partial<OVConfig> = {}): OVConfig {
  return {
    enabled: true,
    endpoint: "http://127.0.0.1:1933",
    apiKey: "",
    account: "",
    user: "",
    agentId: "pi",
    syncTurns: true,
    recallBudget: 2000,
    recallMaxContentChars: 500,
    recallPreferAbstract: true,
    recallLimit: 6,
    recallScoreThreshold: 0.35,
    recallMinQueryLength: 3,
    profileBudget: 10000,
    resumeContextBudget: 2000,
    indexBudget: 2000,
    commitTokenThreshold: 20000,
    commitOnShutdown: true,
    captureToolResults: false,
    captureMode: "semantic",
    captureMaxLength: 24000,
    captureAssistantTurns: true,
    mirrorMemoryWrites: true,
    writeQueueFlushInterval: 0,
    writeQueueFlushThreshold: 999,
    bypassPatterns: [],
    logLevel: "error",
    takeoverEnabled: false,
    takeoverTokenThreshold: 30000,
    takeoverKeepRecentTurns: 3,
    takeoverOverviewBudget: 3000,
    takeoverOverviewPollMs: 2000,
    takeoverOverviewPollMax: 15,
    ...overrides,
  };
}

function makeFakeClient(overrides: Record<string, any> = {}) {
  return {
    createSession: vi.fn().mockResolvedValue(true),
    addMessage: vi.fn().mockResolvedValue(true),
    commitSession: vi.fn().mockResolvedValue({
      task_id: "t-1",
      archive_uri: "ov://archive/abc",
    }),
    ...overrides,
  } as any;
}

// ---------------------------------------------------------------------------
// stripInjectedBlocks
// ---------------------------------------------------------------------------

describe("stripInjectedBlocks", () => {
  it("strips <relevant-memories> blocks", () => {
    const text = "Hello<relevant-memories>\nmem1\nmem2\n</relevant-memories>World";
    expect(stripInjectedBlocks(text)).toBe("HelloWorld");
  });

  it("strips <system-reminder> blocks", () => {
    const text = "prefix<system-reminder>reminder text</system-reminder>suffix";
    expect(stripInjectedBlocks(text)).toBe("prefixsuffix");
  });

  it("strips <openviking-context> blocks", () => {
    const text = "before<openviking-context>profile stuff</openviking-context>after";
    expect(stripInjectedBlocks(text)).toBe("beforeafter");
  });

  it("strips [Subagent Context] until double newline or end", () => {
    const text = "real content\n[Subagent Context] subagent notes here\n\nmore real";
    // The regex matches from [Subagent Context] up to (but not including) \n\n.
    // The \n before the marker and the \n\n lookahead both remain, giving 3 \n total.
    // .trim() removes leading/trailing but not internal whitespace.
    expect(stripInjectedBlocks(text)).toBe("real content\n\n\nmore real");
  });

  it("strips [Subagent Context] at end of string", () => {
    const text = "real content\n[Subagent Context] trailing notes";
    expect(stripInjectedBlocks(text)).toBe("real content");
  });

  it("strips null bytes", () => {
    const text = "hello\x00world";
    expect(stripInjectedBlocks(text)).toBe("helloworld");
  });

  it("strips multiple block types together", () => {
    const text =
      "<relevant-memories>m</relevant-memories>\n" +
      "real question\n" +
      "<system-reminder>s</system-reminder>\n" +
      "<openviking-context>o</openviking-context>\n" +
      "[Subagent Context] sub\n\n" +
      "real answer\x00";
    const result = stripInjectedBlocks(text);
    expect(result).not.toContain("<relevant-memories>");
    expect(result).not.toContain("<system-reminder>");
    expect(result).not.toContain("<openviking-context>");
    expect(result).not.toContain("[Subagent Context]");
    expect(result).not.toContain("\x00");
    expect(result).toContain("real question");
    expect(result).toContain("real answer");
  });

  it("trims whitespace", () => {
    expect(stripInjectedBlocks("  hello  ")).toBe("hello");
  });

  it("handles plain text with no injections", () => {
    expect(stripInjectedBlocks("just normal text")).toBe("just normal text");
  });
});

// ---------------------------------------------------------------------------
// shouldCapture
// ---------------------------------------------------------------------------

describe("shouldCapture", () => {
  const cases: Array<{
    name: string;
    text: string;
    mode: "semantic" | "keyword";
    expected: boolean;
    reason: string;
  }> = [
    { name: "empty", text: "", mode: "semantic", expected: false, reason: "empty" },
    { name: "whitespace only", text: "   ", mode: "semantic", expected: false, reason: "empty" },
    { name: "slash command", text: "/help-me-please-now-extra", mode: "semantic", expected: false, reason: "command" },
    { name: "slash command with args", text: "/commit now please right away", mode: "semantic", expected: false, reason: "command" },
    { name: "punctuation only", text: "!!!???...,,,---***+++", mode: "semantic", expected: false, reason: "non_content" },
    { name: "question only short", text: "What is the exact value of pi in mathematics?", mode: "semantic", expected: false, reason: "question_only" },
    { name: "question only CJK not caught by English regex", text: "这是什么？", mode: "semantic", expected: true, reason: "semantic" },
    { name: "too short English", text: "hi", mode: "semantic", expected: false, reason: "too_short" },
    { name: "too short CJK", text: "你好", mode: "semantic", expected: false, reason: "too_short" },
    { name: "CJK min-length met (4 chars)", text: "你好世界", mode: "semantic", expected: true, reason: "semantic" },
    { name: "normal semantic", text: "I need help with the deployment pipeline setup", mode: "semantic", expected: true, reason: "semantic" },
    { name: "keyword mode with trigger", text: "I prefer using vim over emacs for editing code daily", mode: "keyword", expected: true, reason: "trigger_matched" },
    { name: "keyword mode with email", text: "Contact me at test@example.com for further details about the project", mode: "keyword", expected: true, reason: "trigger_matched" },
    { name: "keyword mode no trigger", text: "The function returns a numeric value of forty two", mode: "keyword", expected: false, reason: "no_trigger" },
    { name: "too long", text: "x".repeat(24001), mode: "semantic", expected: false, reason: "too_long" },
    { name: "decision trigger", text: "We decided to use PostgreSQL for this project database", mode: "keyword", expected: true, reason: "trigger_matched" },
    { name: "my name identity", text: "my name is Zayn and I work on code", mode: "keyword", expected: true, reason: "trigger_matched" },
  ];

  for (const c of cases) {
    it(`${c.name} → ${c.expected} (${c.reason})`, () => {
      const result = shouldCapture(c.text, c.mode);
      expect(result.capture).toBe(c.expected);
      expect(result.reason).toBe(c.reason);
    });
  }
});

// ---------------------------------------------------------------------------
// estimateTokens
// ---------------------------------------------------------------------------

describe("estimateTokens", () => {
  it("returns 0 for empty", () => {
    expect(estimateTokens("")).toBe(0);
  });

  it("estimates English at ~1/4 char", () => {
    // 100 ASCII chars → ceil(100/4) = 25
    expect(estimateTokens("a".repeat(100))).toBe(25);
  });

  it("estimates CJK at 1.5 per char", () => {
    // 10 CJK chars → ceil(10*1.5) = 15
    const cjk = "あ".repeat(10);
    expect(estimateTokens(cjk)).toBe(15);
  });

  it("handles mixed CJK + ASCII", () => {
    // 2 CJK (3.0) + 8 ASCII (2.0) = 5.0 → ceil = 5
    const mixed = "ああ" + "a".repeat(8);
    expect(estimateTokens(mixed)).toBe(5);
  });
});

describe("truncateToTokens", () => {
  it("returns text unchanged when within budget", () => {
    expect(truncateToTokens("hello world", 100)).toBe("hello world");
  });

  it("truncates ASCII to the budget", () => {
    const text = "a".repeat(4000); // ~1000 tokens
    const out = truncateToTokens(text, 100);
    expect(estimateTokens(out)).toBeLessThanOrEqual(100);
    expect(out.length).toBeGreaterThan(0);
  });

  it("respects the budget on CJK text (chars-based slicing would overshoot ~4.5x)", () => {
    const text = "あ".repeat(9000); // ~13500 est. tokens
    const out = truncateToTokens(text, 3000);
    expect(estimateTokens(out)).toBeLessThanOrEqual(3000);
    // budget*3 char slicing would have kept 9000 chars — we must keep ~2000
    expect(out.length).toBeLessThan(2500);
  });

  it("returns empty string for zero budget", () => {
    expect(truncateToTokens("something", 0)).toBe("");
  });
});

// ---------------------------------------------------------------------------
// WriteQueue
// ---------------------------------------------------------------------------

describe("WriteQueue", () => {
  it("flush retries failed writes (re-queues at front with remaining batch)", async () => {
    const client = makeFakeClient({
      addMessage: vi
        .fn()
        .mockResolvedValueOnce(true)   // 1st call: msg1 succeeds
        .mockResolvedValueOnce(false)  // 2nd call: msg2 fails
        .mockResolvedValueOnce(true)   // 3rd call: msg2 retry succeeds
        .mockResolvedValueOnce(true),  // 4th call: msg3 succeeds
    });
    const q = new WriteQueue(client, "sess", 0, 999);

    q.enqueue("user", "msg1");
    q.enqueue("user", "msg2");
    q.enqueue("user", "msg3");

    await q.flush();
    // After first flush: msg1 succeeded (call 1), msg2 failed (call 2),
    // msg2+msg3 re-queued at front. Total calls so far: 2.
    expect(client.addMessage).toHaveBeenCalledTimes(2);

    await q.flush();
    // msg2 retry succeeds (call 3), msg3 succeeds (call 4)
    expect(client.addMessage).toHaveBeenCalledTimes(4);
  });

  it("threshold flush triggers", async () => {
    const client = makeFakeClient();
    const q = new WriteQueue(client, "sess", 0, 3);
    q.start();

    q.enqueue("user", "a");
    q.enqueue("user", "b");
    // Not yet flushed (2 < 3)
    expect(client.addMessage).not.toHaveBeenCalled();

    q.enqueue("user", "c");
    // Give the async flush a tick
    await new Promise((r) => setTimeout(r, 10));

    expect(client.addMessage).toHaveBeenCalled();
    q.cancelPending();
  });

  it("flush is no-op when empty", async () => {
    const client = makeFakeClient();
    const q = new WriteQueue(client, "sess", 0, 999);
    await q.flush();
    expect(client.addMessage).not.toHaveBeenCalled();
  });

  it("cancelPending clears the timer", () => {
    const client = makeFakeClient();
    const q = new WriteQueue(client, "sess", 1000, 999);
    q.start();
    q.cancelPending();
    // Should not throw; no timer leak
  });
});

// ---------------------------------------------------------------------------
// SyncManager — takeoverEnabled=true (faithful mode)
// ---------------------------------------------------------------------------

describe("SyncManager — faithful mode (takeoverEnabled=true)", () => {
  let fakeClient: any;
  let config: OVConfig;

  beforeEach(() => {
    fakeClient = makeFakeClient();
    config = baseConfig({
      takeoverEnabled: true,
      captureMode: "keyword", // should be ignored in faithful mode
      captureAssistantTurns: true,
    });
  });

  async function makeReadySync() {
    const sync = new SyncManager(fakeClient, config);
    await sync.ensureSession("pi-sess-1");
    return sync;
  }

  it("question-only turns ARE synced (captureMode ignored)", async () => {
    const sync = await makeReadySync();
    const tokens = await sync.syncTurn(
      "What is the meaning of life in the universe?", "42", [], 1,
    );
    expect(tokens).toBeGreaterThan(0);
    // Flush the write queue so enqueued messages hit addMessage
    await sync.flushQueue();
    expect(fakeClient.addMessage).toHaveBeenCalled();
  });

  it("slash-commands are skipped", async () => {
    const sync = await makeReadySync();
    const tokens = await sync.syncTurn("/help", "", [], 1);
    expect(tokens).toBe(0);
    expect(fakeClient.addMessage).not.toHaveBeenCalled();
  });

  it("empty content is skipped", async () => {
    const sync = await makeReadySync();
    const tokens = await sync.syncTurn("   ", "", [], 1);
    expect(tokens).toBe(0);
    expect(fakeClient.addMessage).not.toHaveBeenCalled();
  });

  it("syncTurn returns token estimate > 0 for synced turns", async () => {
    const sync = await makeReadySync();
    const tokens = await sync.syncTurn(
      "This is a real user question with enough content",
      "Here is my answer",
      [],
      1,
    );
    expect(tokens).toBeGreaterThan(0);
  });

  it("syncTurn returns 0 for skipped turns", async () => {
    const sync = await makeReadySync();
    const tokens = await sync.syncTurn("/commit", "", [], 1);
    expect(tokens).toBe(0);
  });

  it("internal commit threshold does NOT fire even when pendingTokens exceeds commitTokenThreshold", async () => {
    fakeClient.addMessage = vi.fn().mockResolvedValue(true);
    const sync = await makeReadySync();

    // Send a very large turn to exceed threshold
    const bigText = "x".repeat(100000);
    await sync.syncTurn(bigText, bigText, [], 1);

    // commitSession should NOT have been called
    expect(fakeClient.commitSession).not.toHaveBeenCalled();
  }, 10000);

  it("dedup: same or lower run index skipped (turnIndex is a caller-maintained run counter)", async () => {
    const sync = await makeReadySync();
    const t1 = await sync.syncTurn("hello there my friend", "world", [], 1);
    expect(t1).toBeGreaterThan(0);

    // Same index → skipped
    expect(await sync.syncTurn("hello there my friend", "world", [], 1)).toBe(0);
    // Lower index → skipped
    expect(await sync.syncTurn("another message goes here", "reply", [], 0)).toBe(0);
    // Higher (monotonically increasing) index → synced
    expect(await sync.syncTurn("the next run message", "reply", [], 2)).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// SyncManager — takeoverEnabled=false (normal mode)
// ---------------------------------------------------------------------------

describe("SyncManager — normal mode (takeoverEnabled=false)", () => {
  let fakeClient: any;
  let config: OVConfig;

  beforeEach(() => {
    fakeClient = makeFakeClient();
    config = baseConfig({
      takeoverEnabled: false,
      captureMode: "keyword",
      commitTokenThreshold: 100,
      captureAssistantTurns: true,
    });
  });

  async function makeReadySync() {
    const sync = new SyncManager(fakeClient, config);
    await sync.ensureSession("pi-sess-2");
    return sync;
  }

  it("keyword mode gates capture — trigger matched → captured", async () => {
    const sync = await makeReadySync();
    const tokens = await sync.syncTurn(
      "I prefer using dark mode for the editor",
      "ok",
      [],
      1,
    );
    expect(tokens).toBeGreaterThan(0);
  });

  it("keyword mode gates capture — no trigger → skipped", async () => {
    const sync = await makeReadySync();
    const tokens = await sync.syncTurn(
      "The function returns a numeric value of seven",
      "acknowledged",
      [],
      1,
    );
    expect(tokens).toBe(0);
  });

  it("internal threshold commit fires when pendingTokens exceeds threshold", async () => {
    fakeClient.addMessage = vi.fn().mockResolvedValue(true);
    const sync = await makeReadySync();

    // Big text with a trigger word to pass keyword filter
    const bigText = "I prefer " + "x".repeat(500);
    await sync.syncTurn(bigText, bigText, [], 1);

    // Allow async flush + commit to run
    await new Promise((r) => setTimeout(r, 50));

    expect(fakeClient.commitSession).toHaveBeenCalled();
  }, 10000);

  it("internal threshold commit is skipped when flush fails", async () => {
    fakeClient.addMessage = vi.fn().mockResolvedValue(false);
    const sync = await makeReadySync();

    const bigText = "I prefer " + "x".repeat(500);
    await sync.syncTurn(bigText, bigText, [], 1);

    expect(fakeClient.commitSession).not.toHaveBeenCalled();
  });

  it("commit() returns archive_uri string on success", async () => {
    const sync = await makeReadySync();
    const result = await sync.commit();
    expect(result).toBe("ov://archive/abc");
  });

  it("commit() returns null on failure", async () => {
    fakeClient.commitSession = vi.fn().mockResolvedValue(null);
    const sync = await makeReadySync();
    const result = await sync.commit();
    expect(result).toBeNull();
  });

  it("ensureSession returns false when createSession fails", async () => {
    fakeClient.createSession = vi.fn().mockResolvedValue(false);
    const sync = new SyncManager(fakeClient, config);
    const ok = await sync.ensureSession("fail-sess");
    expect(ok).toBe(false);
    expect(sync.sessionId).toBeNull();
  });

  it("ensureSession idempotent — second call reuses existing session", async () => {
    const sync = await makeReadySync();
    const ok = await sync.ensureSession("pi-sess-2");
    expect(ok).toBe(true);
    // createSession only called once
    expect(fakeClient.createSession).toHaveBeenCalledTimes(1);
  });
});
