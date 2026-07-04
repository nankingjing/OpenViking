import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  flattenContent,
  fingerprintMessage,
  isUserTurnStart,
  countUserTurns,
  findBoundaryIndex,
  TakeoverManager,
  TAKEOVER_ENTRY_TYPE,
  OVERVIEW_MARKER,
  type TakeoverPersistedState,
} from "../takeover.js";
import type { OVConfig } from "../config.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function msg(role: string, content: unknown, timestamp = 0) {
  return { role, content, timestamp } as any;
}

function userMsg(text: string, ts = 0) {
  return msg("user", text, ts);
}

function assistantMsg(text: string, ts = 0) {
  return msg("assistant", text, ts);
}

function toolResultMsg(text: string, ts = 0) {
  return msg("toolResult", text, ts);
}

function overviewMsg(ts = 0) {
  return msg("user", `${OVERVIEW_MARKER} stuff`, ts);
}

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
    takeoverEnabled: true,
    takeoverTokenThreshold: 100,
    takeoverKeepRecentTurns: 2,
    takeoverOverviewBudget: 3000,
    takeoverOverviewPollMs: 1,
    takeoverOverviewPollMax: 2,
    ...overrides,
  };
}

function makeFakeClient(overrides: Record<string, any> = {}) {
  return {
    getSessionContext: vi.fn().mockResolvedValue({ latest_archive_overview: null }),
    ...overrides,
  } as any;
}

function makeFakeSync(overrides: Record<string, any> = {}) {
  return {
    flushQueue: vi.fn().mockResolvedValue(true),
    commit: vi.fn().mockResolvedValue(null),
    sessionId: "test-session",
    ...overrides,
  } as any;
}

// ---------------------------------------------------------------------------
// flattenContent
// ---------------------------------------------------------------------------

describe("flattenContent", () => {
  it("returns string content as-is", () => {
    expect(flattenContent(msg("user", "hello"))).toBe("hello");
  });

  it("returns empty string for non-string non-array content", () => {
    expect(flattenContent(msg("user", 42))).toBe("");
    expect(flattenContent(msg("user", null))).toBe("");
    expect(flattenContent(msg("user", undefined))).toBe("");
  });

  it("extracts text from array of content blocks", () => {
    const m = msg("user", [
      { type: "text", text: "Hello " },
      { type: "image", url: "x.png" },
      { type: "text", text: "world" },
    ]);
    expect(flattenContent(m)).toBe("Hello world");
  });

  it("ignores blocks without text field", () => {
    const m = msg("user", [
      { type: "text", text: "a" },
      { type: "text" },
      { type: "text", text: "b" },
    ]);
    expect(flattenContent(m)).toBe("ab");
  });

  it("handles empty array", () => {
    expect(flattenContent(msg("user", []))).toBe("");
  });

  it("handles null/undefined blocks in array", () => {
    const m = msg("user", [null, undefined, { type: "text", text: "ok" }]);
    expect(flattenContent(m)).toBe("ok");
  });
});

// ---------------------------------------------------------------------------
// fingerprintMessage
// ---------------------------------------------------------------------------

describe("fingerprintMessage", () => {
  it("includes role and content length", () => {
    const fp = fingerprintMessage(userMsg("hello world"));
    expect(fp).toMatch(/^user:/);
    expect(fp).toContain(":11:"); // length of "hello world"
  });

  it("includes content prefix", () => {
    const fp = fingerprintMessage(userMsg("hello world"));
    expect(fp).toContain("hello world");
  });

  it("different roles produce different fingerprints", () => {
    const a = fingerprintMessage(userMsg("hi"));
    const b = fingerprintMessage(assistantMsg("hi"));
    expect(a).not.toBe(b);
  });

  it("same content same role produces same fingerprint", () => {
    const a = fingerprintMessage(userMsg("same"));
    const b = fingerprintMessage(userMsg("same"));
    expect(a).toBe(b);
  });

  it("long content is truncated to 200 chars in fingerprint", () => {
    const long = "a".repeat(500);
    const fp = fingerprintMessage(userMsg(long));
    // fingerprint format: role:length:prefix(200)
    const parts = fp.split(":");
    expect(Number(parts[1])).toBe(500);
    expect(parts[2].length).toBe(200);
  });
});

// ---------------------------------------------------------------------------
// isUserTurnStart
// ---------------------------------------------------------------------------

describe("isUserTurnStart", () => {
  it("returns true for plain user messages", () => {
    expect(isUserTurnStart(userMsg("hello"))).toBe(true);
  });

  it("returns false for assistant messages", () => {
    expect(isUserTurnStart(assistantMsg("hi"))).toBe(false);
  });

  it("returns false for toolResult messages", () => {
    expect(isUserTurnStart(toolResultMsg("result"))).toBe(false);
  });

  it("returns false for our injected overview marker", () => {
    expect(isUserTurnStart(overviewMsg())).toBe(false);
  });

  it("returns false for custom/bashExecution roles", () => {
    expect(isUserTurnStart(msg("custom", "data"))).toBe(false);
    expect(isUserTurnStart(msg("bashExecution", "output"))).toBe(false);
  });

  it("returns true for user message with array content not starting with marker", () => {
    const m = msg("user", [{ type: "text", text: "real question" }]);
    expect(isUserTurnStart(m)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// countUserTurns
// ---------------------------------------------------------------------------

describe("countUserTurns", () => {
  it("counts zero for empty list", () => {
    expect(countUserTurns([])).toBe(0);
  });

  it("counts only real user turns, not overview markers", () => {
    const msgs = [
      userMsg("turn 1"),
      assistantMsg("reply 1"),
      userMsg("turn 2"),
      assistantMsg("reply 2"),
      overviewMsg(),
      userMsg("turn 3"),
    ];
    expect(countUserTurns(msgs)).toBe(3);
  });

  it("never counts toolResult, assistant, custom, bashExecution", () => {
    const msgs = [
      userMsg("q"),
      assistantMsg("a"),
      toolResultMsg("r"),
      msg("custom", "x"),
      msg("bashExecution", "y"),
      userMsg("q2"),
    ];
    expect(countUserTurns(msgs)).toBe(2);
  });

  it("counts user messages with array content", () => {
    const msgs = [
      msg("user", [{ type: "text", text: "hello" }]),
      assistantMsg("a"),
      msg("user", [{ type: "text", text: "world" }]),
    ];
    expect(countUserTurns(msgs)).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// findBoundaryIndex
// ---------------------------------------------------------------------------

describe("findBoundaryIndex", () => {
  it("returns -1 when list is empty", () => {
    expect(findBoundaryIndex([], 0)).toBe(-1);
  });

  it("returns index of first user turn when covered=0", () => {
    const msgs = [userMsg("t1"), assistantMsg("a")];
    expect(findBoundaryIndex(msgs, 0)).toBe(0);
  });

  it("returns index of (n+1)th user turn start", () => {
    const msgs = [
      userMsg("t1"),       // 0
      assistantMsg("a1"),  // 1
      userMsg("t2"),       // 2 — 2nd user turn
      assistantMsg("a2"),  // 3
      userMsg("t3"),       // 4 — 3rd user turn
    ];
    // covered=1 means we want the start of the 2nd turn → index 2
    expect(findBoundaryIndex(msgs, 1)).toBe(2);
    // covered=2 means we want the start of the 3rd turn → index 4
    expect(findBoundaryIndex(msgs, 2)).toBe(4);
  });

  it("returns -1 when fewer turns than covered+1 exist", () => {
    const msgs = [userMsg("t1"), assistantMsg("a1")];
    expect(findBoundaryIndex(msgs, 3)).toBe(-1);
  });

  it("skips overview-marker user messages", () => {
    const msgs = [
      overviewMsg(),       // NOT counted
      userMsg("t1"),       // 1st real turn
      assistantMsg("a1"),
      userMsg("t2"),       // 2nd real turn
    ];
    // covered=0 → first real turn at index 1
    expect(findBoundaryIndex(msgs, 0)).toBe(1);
    // covered=1 → second real turn at index 3
    expect(findBoundaryIndex(msgs, 1)).toBe(3);
  });

  it("skips toolResult/assistant/custom/bashExecution between turns", () => {
    const msgs = [
      userMsg("t1"),
      assistantMsg("a1"),
      toolResultMsg("r1"),
      msg("custom", "x"),
      msg("bashExecution", "y"),
      userMsg("t2"),
    ];
    expect(findBoundaryIndex(msgs, 1)).toBe(5);
  });

  it("returns -1 when covered equals total user turns", () => {
    const msgs = [userMsg("t1"), userMsg("t2")];
    expect(findBoundaryIndex(msgs, 2)).toBe(-1);
  });
});

// ---------------------------------------------------------------------------
// TakeoverManager.transformContext
// ---------------------------------------------------------------------------

describe("TakeoverManager.transformContext", () => {
  let appendEntry: ReturnType<typeof vi.fn>;
  let fakeClient: ReturnType<typeof makeFakeClient>;
  let fakeSync: ReturnType<typeof makeFakeSync>;

  function makeManager(config: OVConfig) {
    appendEntry = vi.fn();
    return new TakeoverManager({
      client: fakeClient,
      sync: fakeSync,
      config,
      appendEntry,
    });
  }

  beforeEach(() => {
    fakeClient = makeFakeClient();
    fakeSync = makeFakeSync();
  });

  it("(a) disabled → passthrough", () => {
    const mgr = makeManager(baseConfig({ takeoverEnabled: false }));
    const msgs = [userMsg("hi"), assistantMsg("hello")];
    const result = mgr.transformContext(msgs);
    expect(result).toBe(msgs);
  });

  it("(b) no boundary (coveredUserTurns=0) → passthrough", () => {
    const mgr = makeManager(baseConfig());
    const msgs = [userMsg("hi"), assistantMsg("hello")];
    const result = mgr.transformContext(msgs);
    expect(result).toBe(msgs);
  });

  it("(c) boundary=2, overview set → drops first 2 turns, injects overview", () => {
    const mgr = makeManager(baseConfig({ takeoverKeepRecentTurns: 1 }));
    // Build a manager with internal state by restoring
    mgr.restore([{
      type: "custom",
      customType: TAKEOVER_ENTRY_TYPE,
      data: { coveredUserTurns: 2, overview: "Session summary here." },
    }]);

    const msgs = [
      userMsg("turn 1 question", 1000),
      assistantMsg("turn 1 answer", 1001),
      userMsg("turn 2 question", 2000),
      assistantMsg("turn 2 answer", 2001),
      userMsg("turn 3 question", 3000),
      assistantMsg("turn 3 answer", 3001),
    ];

    const result = mgr.transformContext(msgs);

    // Overview message injected at index 0
    expect(result[0].role).toBe("user");
    expect(flattenContent(result[0])).toContain(OVERVIEW_MARKER);
    expect(flattenContent(result[0])).toContain("Session summary here.");

    // Only turn 3 messages kept
    expect(result.length).toBe(3); // overview + user3 + assistant3
    expect(flattenContent(result[1])).toBe("turn 3 question");
    expect(flattenContent(result[2])).toBe("turn 3 answer");
  });

  it("(c) toolResult pairing preserved after boundary", () => {
    const mgr = makeManager(baseConfig({ takeoverKeepRecentTurns: 1 }));
    mgr.restore([{
      type: "custom",
      customType: TAKEOVER_ENTRY_TYPE,
      data: { coveredUserTurns: 1, overview: "summary" },
    }]);

    const msgs = [
      userMsg("turn 1", 1000),
      assistantMsg("answer 1", 1001),
      userMsg("turn 2", 2000),
      assistantMsg("answer 2 with tool", 2001),
      toolResultMsg("tool output 2", 2002),
    ];

    const result = mgr.transformContext(msgs);
    // overview + turn2 user + turn2 assistant + toolResult
    expect(result.length).toBe(4);
    expect(flattenContent(result[1])).toBe("turn 2");
    expect(flattenContent(result[2])).toBe("answer 2 with tool");
    expect(result[3].role).toBe("toolResult");
  });

  it("(d) byte-stability: two consecutive calls produce deeply equal output", () => {
    const mgr = makeManager(baseConfig({ takeoverKeepRecentTurns: 1 }));
    mgr.restore([{
      type: "custom",
      customType: TAKEOVER_ENTRY_TYPE,
      data: { coveredUserTurns: 1, overview: "stable summary" },
    }]);

    const msgs = [
      userMsg("turn 1", 1000),
      assistantMsg("a1", 1001),
      userMsg("turn 2", 2000),
    ];

    const r1 = mgr.transformContext(msgs);
    const r2 = mgr.transformContext(msgs);
    expect(r1).toEqual(r2);
    // Also check the overview message timestamp is deterministic
    expect(r1[0].timestamp).toBe(r2[0].timestamp);
  });

  it("(e) fingerprint mismatch → boundary reset, passthrough", () => {
    const mgr = makeManager(baseConfig({ takeoverKeepRecentTurns: 1 }));
    mgr.restore([{
      type: "custom",
      customType: TAKEOVER_ENTRY_TYPE,
      data: { coveredUserTurns: 1, overview: "summary" },
    }]);

    const msgs1 = [
      userMsg("original turn 1", 1000),
      assistantMsg("a1", 1001),
      userMsg("turn 2", 2000),
    ];
    // First call establishes fingerprint from messages[boundaryIdx-1]
    // With coveredUserTurns=1, boundaryIdx=2 (index of "turn 2"),
    // so fingerprint is of messages[1] = assistantMsg("a1")
    mgr.transformContext(msgs1);

    // Mutate the last-covered message (the assistant reply at index 1,
    // which is messages[boundaryIdx-1]) to trigger mismatch.
    const msgs2 = [
      userMsg("original turn 1", 1000),
      assistantMsg("MUTATED assistant reply", 1001),
      userMsg("turn 2", 2000),
    ];
    const result = mgr.transformContext(msgs2);

    // Should be passthrough (boundary reset)
    expect(result).toBe(msgs2);
    expect(result.length).toBe(3);
    // No overview marker in any message
    for (const m of result) {
      expect(flattenContent(m).startsWith(OVERVIEW_MARKER)).toBe(false);
    }
  });

  it("(e) subsequent calls after fingerprint mismatch stay passthrough", () => {
    const mgr = makeManager(baseConfig({ takeoverKeepRecentTurns: 1 }));
    mgr.restore([{
      type: "custom",
      customType: TAKEOVER_ENTRY_TYPE,
      data: { coveredUserTurns: 1, overview: "summary" },
    }]);

    const msgs1 = [userMsg("orig", 1000), userMsg("t2", 2000)];
    mgr.transformContext(msgs1);

    const msgs2 = [userMsg("MUTATED", 1000), userMsg("t2", 2000)];
    mgr.transformContext(msgs2); // triggers reset

    const msgs3 = [userMsg("MUTATED", 1000), userMsg("t2", 2000)];
    const result = mgr.transformContext(msgs3);
    expect(result).toBe(msgs3); // still passthrough
  });

  it("(f) history shorter than boundary → reset + passthrough", () => {
    const mgr = makeManager(baseConfig({ takeoverKeepRecentTurns: 1 }));
    mgr.restore([{
      type: "custom",
      customType: TAKEOVER_ENTRY_TYPE,
      data: { coveredUserTurns: 5, overview: "big summary" },
    }]);

    const msgs = [userMsg("only turn", 1000)];
    const result = mgr.transformContext(msgs);
    expect(result).toBe(msgs);
    expect(result.length).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// TakeoverManager.onTurnSynced / commitAndAdvance
// ---------------------------------------------------------------------------

describe("TakeoverManager.onTurnSynced + commitAndAdvance", () => {
  let appendEntry: ReturnType<typeof vi.fn>;
  let fakeClient: any;
  let fakeSync: any;

  function makeManager(config: OVConfig) {
    appendEntry = vi.fn();
    return new TakeoverManager({
      client: fakeClient,
      sync: fakeSync,
      config,
      appendEntry,
    });
  }

  beforeEach(() => {
    appendEntry = vi.fn();
    fakeClient = makeFakeClient({
      getSessionContext: vi.fn().mockResolvedValue({
        latest_archive_overview: "  fresh overview text  ",
      }),
    });
    fakeSync = makeFakeSync({
      commit: vi.fn().mockResolvedValue("ov://archive/abc"),
    });
  });

  it("threshold not reached → no commit", async () => {
    const mgr = makeManager(baseConfig({ takeoverTokenThreshold: 1000 }));
    // Need lastSeenUserTurns to be set via transformContext
    mgr.transformContext([userMsg("q"), assistantMsg("a")]);
    await mgr.onTurnSynced(100);
    expect(fakeSync.commit).not.toHaveBeenCalled();
  });

  it("threshold reached but lastSeenUserTurns <= keepRecentTurns → no commit", async () => {
    const mgr = makeManager(baseConfig({
      takeoverTokenThreshold: 50,
      takeoverKeepRecentTurns: 3,
    }));
    // Only 1 user turn seen
    mgr.transformContext([userMsg("q"), assistantMsg("a")]);
    await mgr.onTurnSynced(200);
    expect(fakeSync.commit).not.toHaveBeenCalled();
  });

  it("commit success + overview ready → boundary advances, pendingTokens reset, appendEntry called", async () => {
    const mgr = makeManager(baseConfig({
      takeoverTokenThreshold: 50,
      takeoverKeepRecentTurns: 1,
    }));
    // 3 user turns → lastSeenUserTurns=3
    mgr.transformContext([
      userMsg("t1"), assistantMsg("a1"),
      userMsg("t2"), assistantMsg("a2"),
      userMsg("t3"),
    ]);
    await mgr.onTurnSynced(200);

    expect(fakeSync.flushQueue).toHaveBeenCalled();
    expect(fakeSync.commit).toHaveBeenCalled();
    expect(fakeClient.getSessionContext).toHaveBeenCalled();

    const state = mgr.state;
    // newCovered = 3 - 1 = 2
    expect(state.coveredUserTurns).toBe(2);
    expect(state.pendingTokens).toBe(0);
    expect(state.overview).toBe("fresh overview text");

    expect(appendEntry).toHaveBeenCalledWith(
      TAKEOVER_ENTRY_TYPE,
      expect.objectContaining({
        coveredUserTurns: 2,
        overview: "fresh overview text",
        pendingTokens: 0,
      } satisfies TakeoverPersistedState),
    );
  });

  it("shutdown persists once — identical state does not append redundant entries", async () => {
    const mgr = makeManager(baseConfig({
      takeoverTokenThreshold: 50,
      takeoverKeepRecentTurns: 1,
    }));
    mgr.transformContext([
      userMsg("t1"), assistantMsg("a1"),
      userMsg("t2"), assistantMsg("a2"),
      userMsg("t3"),
    ]);
    await mgr.onTurnSynced(200); // commits + persists once
    const callsAfterCommit = appendEntry.mock.calls.length;
    await mgr.shutdown(); // state unchanged → no new entry
    await mgr.shutdown();
    expect(appendEntry.mock.calls.length).toBe(callsAfterCommit);
  });

  it("persisted overview is capped to the overview budget (CJK-safe)", async () => {
    const hugeCjk = "あ".repeat(9000); // ~13.5k est. tokens
    fakeClient.getSessionContext = vi.fn().mockResolvedValue({
      latest_archive_overview: hugeCjk,
      messages: [],
    });
    const mgr = makeManager(baseConfig({
      takeoverTokenThreshold: 50,
      takeoverKeepRecentTurns: 1,
      takeoverOverviewBudget: 1000,
    }));
    mgr.transformContext([
      userMsg("t1"), assistantMsg("a1"),
      userMsg("t2"), assistantMsg("a2"),
      userMsg("t3"),
    ]);
    await mgr.onTurnSynced(200);
    const entry = appendEntry.mock.calls.find((c: any[]) => c[0] === TAKEOVER_ENTRY_TYPE)?.[1];
    expect(entry).toBeDefined();
    // 1000-token budget ≈ 666 CJK chars, nowhere near the raw 9000
    expect(entry.overview.length).toBeLessThan(1000);
  });

  it("flush returns false → no commit, boundary unchanged, pendingTokens retained", async () => {
    fakeSync.flushQueue = vi.fn().mockResolvedValue(false);
    const mgr = makeManager(baseConfig({
      takeoverTokenThreshold: 50,
      takeoverKeepRecentTurns: 1,
    }));
    mgr.transformContext([
      userMsg("t1"), assistantMsg("a1"),
      userMsg("t2"),
    ]);
    await mgr.onTurnSynced(200);

    expect(fakeSync.flushQueue).toHaveBeenCalled();
    expect(fakeSync.commit).not.toHaveBeenCalled();
    const state = mgr.state;
    expect(state.coveredUserTurns).toBe(0);
    expect(state.pendingTokens).toBe(200);
    expect(appendEntry).not.toHaveBeenCalled();
  });

  it("commit returns null → boundary unchanged, pendingTokens retained", async () => {
    fakeSync.commit = vi.fn().mockResolvedValue(null);
    const mgr = makeManager(baseConfig({
      takeoverTokenThreshold: 50,
      takeoverKeepRecentTurns: 1,
    }));
    mgr.transformContext([
      userMsg("t1"), assistantMsg("a1"),
      userMsg("t2"),
    ]);
    await mgr.onTurnSynced(200);

    const state = mgr.state;
    expect(state.coveredUserTurns).toBe(0);
    expect(state.pendingTokens).toBe(200); // retained
    expect(appendEntry).not.toHaveBeenCalled();
  });

  it("overview never ready → boundary unchanged but pendingTokens reset", async () => {
    fakeClient.getSessionContext = vi.fn().mockResolvedValue({
      latest_archive_overview: "",
    });
    const mgr = makeManager(baseConfig({
      takeoverTokenThreshold: 50,
      takeoverKeepRecentTurns: 1,
      takeoverOverviewPollMax: 2,
      takeoverOverviewPollMs: 1,
    }));
    mgr.transformContext([
      userMsg("t1"), assistantMsg("a1"),
      userMsg("t2"),
    ]);
    await mgr.onTurnSynced(200);

    const state = mgr.state;
    expect(state.coveredUserTurns).toBe(0);
    expect(state.pendingTokens).toBe(0); // reset per design
  });

  it("concurrent commitAndAdvance → second no-ops", async () => {
    let resolveCommit: (v: any) => void;
    const commitPromise = new Promise<any>((r) => { resolveCommit = r; });
    fakeSync.commit = vi.fn().mockReturnValue(commitPromise);
    // getSessionContext also needs to be slow or fast — overview polling
    fakeClient.getSessionContext = vi.fn().mockResolvedValue({
      latest_archive_overview: "overview",
    });

    const mgr = makeManager(baseConfig({
      takeoverTokenThreshold: 50,
      takeoverKeepRecentTurns: 1,
    }));
    mgr.transformContext([
      userMsg("t1"), assistantMsg("a1"),
      userMsg("t2"),
    ]);

    // Fire two without awaiting first
    const p1 = mgr.commitAndAdvance();
    const p2 = mgr.commitAndAdvance();

    // Second should return false immediately (no-op)
    expect(await p2).toBe(false);

    // Now resolve the commit
    resolveCommit!("ov://archive/abc");
    const r1 = await p1;
    expect(r1).toBe(true);
    // commit was only called once
    expect(fakeSync.commit).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// TakeoverManager.handleBeforeCompact
// ---------------------------------------------------------------------------

describe("TakeoverManager.handleBeforeCompact", () => {
  let appendEntry: ReturnType<typeof vi.fn>;
  let fakeClient: any;
  let fakeSync: any;

  function makeManager(config: OVConfig) {
    appendEntry = vi.fn();
    return new TakeoverManager({
      client: fakeClient,
      sync: fakeSync,
      config,
      appendEntry,
    });
  }

  beforeEach(() => {
    appendEntry = vi.fn();
    fakeClient = makeFakeClient({
      getSessionContext: vi.fn().mockResolvedValue({
        latest_archive_overview: "compact overview",
      }),
    });
    fakeSync = makeFakeSync({
      commit: vi.fn().mockResolvedValue("ov://archive/compact"),
    });
  });

  it("returns compaction with overview + firstKeptEntryId passthrough", async () => {
    const mgr = makeManager(baseConfig());
    const result = await mgr.handleBeforeCompact({
      firstKeptEntryId: "entry-42",
      tokensBefore: 50000,
    });

    expect(result).toBeDefined();
    expect(result!.compaction.summary).toContain(OVERVIEW_MARKER);
    expect(result!.compaction.summary).toContain("compact overview");
    expect(result!.compaction.firstKeptEntryId).toBe("entry-42");
    expect(result!.compaction.tokensBefore).toBe(50000);
    expect(result!.compaction.details.source).toBe("openviking");
  });

  it("returns undefined when firstKeptEntryId missing", async () => {
    const mgr = makeManager(baseConfig());
    const result = await mgr.handleBeforeCompact({
      firstKeptEntryId: undefined,
      tokensBefore: 50000,
    });
    expect(result).toBeUndefined();
  });

  it("returns undefined when flush fails", async () => {
    fakeSync.flushQueue = vi.fn().mockResolvedValue(false);
    const mgr = makeManager(baseConfig());
    const result = await mgr.handleBeforeCompact({
      firstKeptEntryId: "entry-1",
      tokensBefore: 1000,
    });
    expect(result).toBeUndefined();
    expect(fakeSync.commit).not.toHaveBeenCalled();
  });

  it("returns undefined when commit fails", async () => {
    fakeSync.commit = vi.fn().mockResolvedValue(null);
    const mgr = makeManager(baseConfig());
    const result = await mgr.handleBeforeCompact({
      firstKeptEntryId: "entry-1",
      tokensBefore: 1000,
    });
    expect(result).toBeUndefined();
  });

  it("returns undefined when overview missing", async () => {
    fakeClient.getSessionContext = vi.fn().mockResolvedValue({
      latest_archive_overview: "",
    });
    const mgr = makeManager(baseConfig({
      takeoverOverviewPollMax: 1,
      takeoverOverviewPollMs: 1,
    }));
    const result = await mgr.handleBeforeCompact({
      firstKeptEntryId: "entry-1",
      tokensBefore: 1000,
    });
    expect(result).toBeUndefined();
  });

  it("boundary reset after success", async () => {
    const mgr = makeManager(baseConfig());
    // Pre-set some state via restore
    mgr.restore([{
      type: "custom",
      customType: TAKEOVER_ENTRY_TYPE,
      data: { coveredUserTurns: 3, overview: "old" },
    }]);

    await mgr.handleBeforeCompact({
      firstKeptEntryId: "entry-99",
      tokensBefore: 8000,
    });

    expect(mgr.state.coveredUserTurns).toBe(0);
  });

  it("returns undefined when disabled", async () => {
    const mgr = makeManager(baseConfig({ takeoverEnabled: false }));
    const result = await mgr.handleBeforeCompact({
      firstKeptEntryId: "entry-1",
      tokensBefore: 1000,
    });
    expect(result).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// TakeoverManager.restore
// ---------------------------------------------------------------------------

describe("TakeoverManager.restore", () => {
  function makeManager() {
    return new TakeoverManager({
      client: makeFakeClient(),
      sync: makeFakeSync(),
      config: baseConfig(),
      appendEntry: vi.fn(),
    });
  }

  it("reads last ov-takeover custom entry from entries array", () => {
    const mgr = makeManager();
    mgr.restore([
      { type: "message" },
      { type: "custom", customType: "other", data: { x: 1 } },
      {
        type: "custom",
        customType: TAKEOVER_ENTRY_TYPE,
        data: { coveredUserTurns: 4, overview: "restored summary" },
      },
    ]);
    expect(mgr.state.coveredUserTurns).toBe(4);
    expect(mgr.state.overview).toBe("restored summary");
  });

  it("uses the last (most recent) matching entry", () => {
    const mgr = makeManager();
    mgr.restore([
      {
        type: "custom",
        customType: TAKEOVER_ENTRY_TYPE,
        data: { coveredUserTurns: 1, overview: "old" },
      },
      {
        type: "custom",
        customType: TAKEOVER_ENTRY_TYPE,
        data: { coveredUserTurns: 7, overview: "newer" },
      },
    ]);
    expect(mgr.state.coveredUserTurns).toBe(7);
    expect(mgr.state.overview).toBe("newer");
  });

  it("ignores malformed data (non-number coveredUserTurns)", () => {
    const mgr = makeManager();
    mgr.restore([
      {
        type: "custom",
        customType: TAKEOVER_ENTRY_TYPE,
        data: { coveredUserTurns: "bad", overview: "x" },
      },
    ]);
    expect(mgr.state.coveredUserTurns).toBe(0);
  });

  it("ignores negative coveredUserTurns", () => {
    const mgr = makeManager();
    mgr.restore([
      {
        type: "custom",
        customType: TAKEOVER_ENTRY_TYPE,
        data: { coveredUserTurns: -1, overview: "x" },
      },
    ]);
    expect(mgr.state.coveredUserTurns).toBe(0);
  });

  it("ignores entries without data", () => {
    const mgr = makeManager();
    mgr.restore([
      { type: "custom", customType: TAKEOVER_ENTRY_TYPE },
    ]);
    expect(mgr.state.coveredUserTurns).toBe(0);
  });

  it("handles empty entries array", () => {
    const mgr = makeManager();
    mgr.restore([]);
    expect(mgr.state.coveredUserTurns).toBe(0);
  });

  it("non-string overview defaults to empty", () => {
    const mgr = makeManager();
    mgr.restore([
      {
        type: "custom",
        customType: TAKEOVER_ENTRY_TYPE,
        data: { coveredUserTurns: 2, overview: 123 },
      },
    ]);
    expect(mgr.state.overview).toBe("");
    expect(mgr.state.coveredUserTurns).toBe(2);
  });

  it("restores pendingTokens (carried across processes)", () => {
    const mgr = makeManager();
    mgr.restore([
      {
        type: "custom",
        customType: TAKEOVER_ENTRY_TYPE,
        data: { coveredUserTurns: 1, overview: "ov", pendingTokens: 4321 },
      },
    ]);
    expect(mgr.state.pendingTokens).toBe(4321);
  });

  it("non-number or negative pendingTokens defaults to 0", () => {
    const mgr = makeManager();
    mgr.restore([
      {
        type: "custom",
        customType: TAKEOVER_ENTRY_TYPE,
        data: { coveredUserTurns: 1, overview: "ov", pendingTokens: "lots" },
      },
    ]);
    expect(mgr.state.pendingTokens).toBe(0);

    const mgr2 = makeManager();
    mgr2.restore([
      {
        type: "custom",
        customType: TAKEOVER_ENTRY_TYPE,
        data: { coveredUserTurns: 1, overview: "ov", pendingTokens: -50 },
      },
    ]);
    expect(mgr2.state.pendingTokens).toBe(0);
  });

  it("missing pendingTokens (old-format entry) defaults to 0", () => {
    const mgr = makeManager();
    mgr.restore([
      {
        type: "custom",
        customType: TAKEOVER_ENTRY_TYPE,
        data: { coveredUserTurns: 3, overview: "legacy entry" },
      },
    ]);
    expect(mgr.state.coveredUserTurns).toBe(3);
    expect(mgr.state.pendingTokens).toBe(0);
  });
});
