import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createServer, type Server } from "node:http";
import { OVClient } from "../client.js";
import type { OVConfig } from "../config.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function baseConfig(overrides: Partial<OVConfig> = {}): OVConfig {
  return {
    enabled: true,
    endpoint: "", // set per test
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
    writeQueueFlushInterval: 5000,
    writeQueueFlushThreshold: 5,
    bypassPatterns: [],
    logLevel: "error",
    takeoverEnabled: true,
    takeoverTokenThreshold: 30000,
    takeoverKeepRecentTurns: 3,
    takeoverOverviewBudget: 3000,
    takeoverOverviewPollMs: 2000,
    takeoverOverviewPollMax: 15,
    ...overrides,
  };
}

type Handler = (req: { url: string; method: string; headers: Record<string, string>; body: any }) => {
  status?: number;
  body?: any;
  rawBody?: string;
  contentType?: string;
};

function startMockServer(handler: Handler): Promise<{ server: Server; port: number }> {
  return new Promise((resolve) => {
    const server = createServer(async (req, res) => {
      let body = "";
      for await (const chunk of req) body += chunk;
      let parsed: any = body;
      try { parsed = body ? JSON.parse(body) : {}; } catch { /* keep raw */ }

      const headers = Object.fromEntries(
        Object.entries(req.headers).map(([k, v]) => [k, Array.isArray(v) ? v[0] : String(v)]),
      );

      const result = await handler({
        url: req.url || "/",
        method: req.method || "GET",
        headers,
        body: parsed,
      });

      const status = result.status ?? 200;
      const contentType = result.contentType ?? "application/json";
      res.writeHead(status, { "Content-Type": contentType });

      if (result.rawBody !== undefined) {
        res.end(result.rawBody);
      } else if (result.body !== undefined) {
        res.end(JSON.stringify(result.body));
      } else {
        res.end(JSON.stringify({ status: "ok", result: null }));
      }
    });
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address() as { port: number };
      resolve({ server, port: addr.port });
    });
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("OVClient — envelope parsing", () => {
  let server: Server;
  let port: number;
  let client: OVClient;

  afterEach(() => {
    server?.close();
  });

  it("parses {status:'ok', result:...} envelope", async () => {
    ({ server, port } = await startMockServer((req) => {
      if (req.url === "/health") {
        return { body: { status: "ok", result: { healthy: true } } };
      }
      return { body: { status: "ok", result: null } };
    }));
    client = new OVClient(baseConfig({ endpoint: `http://127.0.0.1:${port}` }));
    const ok = await client.health();
    expect(ok).toBe(true);
    expect(client.connected).toBe(true);
  });

  it("parses {status:'error', error:...} envelope as failure", async () => {
    ({ server, port } = await startMockServer((req) => {
      if (req.url.startsWith("/api/v1/sessions")) {
        return {
          body: { status: "error", error: { message: "session not found" } },
        };
      }
      return { body: { status: "ok", result: null } };
    }));
    client = new OVClient(baseConfig({ endpoint: `http://127.0.0.1:${port}` }));
    const result = await client.getSession("nonexistent");
    expect(result).toBeNull();
  });

  it("HTTP error status → null/false return", async () => {
    ({ server, port } = await startMockServer(() => {
      return { status: 500, body: { status: "error", error: { message: "internal" } } };
    }));
    client = new OVClient(baseConfig({ endpoint: `http://127.0.0.1:${port}` }));
    const ok = await client.createSession("test");
    expect(ok).toBe(false);
  });

  it("non-JSON response body handled gracefully", async () => {
    ({ server, port } = await startMockServer(() => {
      return { rawBody: "<html>not json</html>", contentType: "text/html" };
    }));
    client = new OVClient(baseConfig({ endpoint: `http://127.0.0.1:${port}` }));
    const ok = await client.health();
    // HTML response with 200 — fetchJSON treats non-JSON as empty body,
    // resp.ok is true but body.status is undefined (not "error"),
    // so it returns ok:true with result:body
    expect(ok).toBe(true);
  });
});

describe("OVClient — timeout", () => {
  let server: Server;

  afterEach(() => {
    server?.close();
  });

  it("request times out when server never responds", async () => {
    // Server accepts connection but never sends response
    server = createServer(() => {
      // never respond — let the client timeout
    });
    await new Promise<void>((resolve) => {
      server.listen(0, "127.0.0.1", () => resolve());
    });
    const addr = server.address() as { port: number };
    const client = new OVClient(baseConfig({
      endpoint: `http://127.0.0.1:${addr.port}`,
    }));
    // health uses 5000ms timeout — we can't easily change it,
    // but we can test that it eventually returns false
    // Instead, test with a method that has a short timeout path.
    // Since timeout is hardcoded in fetchJSON, we verify the behavior:
    // the call should not throw and should return false.
    const ok = await client.health();
    expect(ok).toBe(false);
  }, 15000);
});

describe("OVClient — happy paths", () => {
  let server: Server;
  let port: number;
  let client: OVClient;
  let capturedHeaders: Record<string, string> = {};
  let capturedBody: any = null;
  let capturedUrl = "";
  let capturedMethod = "";

  beforeEach(async () => {
    capturedHeaders = {};
    capturedBody = null;
    capturedUrl = "";
    capturedMethod = "";

    ({ server, port } = await startMockServer((req) => {
      capturedHeaders = req.headers;
      capturedBody = req.body;
      capturedUrl = req.url;
      capturedMethod = req.method;

      if (req.url === "/health") {
        return { body: { status: "ok", result: { healthy: true } } };
      }
      if (req.url === "/api/v1/sessions" && req.method === "POST") {
        return { body: { status: "ok", result: { session_id: req.body.session_id } } };
      }
      if (req.url.startsWith("/api/v1/sessions/") && req.url.endsWith("/messages") && req.method === "POST") {
        return { body: { status: "ok", result: { added: true } } };
      }
      if (req.url.startsWith("/api/v1/sessions/") && req.url.endsWith("/commit") && req.method === "POST") {
        return { body: { status: "ok", result: { task_id: "t-123", archive_uri: "ov://archive/xyz" } } };
      }
      if (req.url.startsWith("/api/v1/sessions/") && req.url.includes("/context")) {
        return {
          body: {
            status: "ok",
            result: {
              latest_archive_overview: "overview text here",
              pre_archive_abstracts: [],
              messages: [],
              estimatedTokens: 100,
              stats: { totalArchives: 1, includedArchives: 1, droppedArchives: 0, failedArchives: 0, activeTokens: 100, archiveTokens: 0 },
            },
          },
        };
      }
      if (req.url.startsWith("/api/v1/search/find")) {
        return {
          body: {
            status: "ok",
            result: {
              memories: [
                { uri: "viking://mem/1", score: 0.9, abstract: "mem1", level: 2, category: "preference", context_type: "memory", overview: null, match_reason: "semantic" },
              ],
              resources: [],
              skills: [],
              total: 1,
            },
          },
        };
      }
      return { body: { status: "ok", result: null } };
    }));

    client = new OVClient(baseConfig({
      endpoint: `http://127.0.0.1:${port}`,
      apiKey: "test-api-key-123",
    }));
  });

  afterEach(() => {
    server?.close();
  });

  it("health() returns true on ok response", async () => {
    const ok = await client.health();
    expect(ok).toBe(true);
  });

  it("createSession() sends correct body and returns true", async () => {
    const ok = await client.createSession("pi-test-1");
    expect(ok).toBe(true);
    expect(capturedMethod).toBe("POST");
    expect(capturedBody.session_id).toBe("pi-test-1");
  });

  it("addMessage() sends role and content", async () => {
    const ok = await client.addMessage("sess-1", "user", "hello world");
    expect(ok).toBe(true);
    expect(capturedBody.role).toBe("user");
    expect(capturedBody.content).toBe("hello world");
  });

  it("commitSession() returns task_id + archive_uri", async () => {
    const result = await client.commitSession("sess-1");
    expect(result).not.toBeNull();
    expect(result!.task_id).toBe("t-123");
    expect(result!.archive_uri).toBe("ov://archive/xyz");
  });

  it("getSessionContext() returns parsed context with overview", async () => {
    const ctx = await client.getSessionContext("sess-1", 2000);
    expect(ctx).not.toBeNull();
    expect(ctx!.latest_archive_overview).toBe("overview text here");
    expect(ctx!.estimatedTokens).toBe(100);
  });

  it("getSessionContext() sends token_budget query param", async () => {
    await client.getSessionContext("sess-1", 5000);
    expect(capturedUrl).toContain("token_budget=5000");
  });

  it("Authorization header present when apiKey set", async () => {
    await client.health();
    expect(capturedHeaders["authorization"]).toBe("Bearer test-api-key-123");
  });

  it("X-OpenViking-Agent header sent", async () => {
    await client.health();
    expect(capturedHeaders["x-openviking-agent"]).toBe("pi");
  });

  it("no Authorization header when apiKey empty", async () => {
    const noKeyClient = new OVClient(baseConfig({
      endpoint: `http://127.0.0.1:${port}`,
      apiKey: "",
    }));
    await noKeyClient.health();
    expect(capturedHeaders["authorization"]).toBeUndefined();
  });

  it("find() returns ranked results from all buckets", async () => {
    const results = await client.find("test query");
    expect(results.length).toBeGreaterThan(0);
    expect(results[0].uri).toBe("viking://mem/1");
    expect(results[0].score).toBe(0.9);
  });
});

describe("OVClient — connection refused", () => {
  it("returns false/empty when server is down", async () => {
    // Port 19337 — unlikely to be in use
    const client = new OVClient(baseConfig({ endpoint: "http://127.0.0.1:19337" }));
    const ok = await client.health();
    expect(ok).toBe(false);
    expect(client.connected).toBe(false);
  });
});

describe("OVClient — ls", () => {
  let server: Server;
  let port: number;

  afterEach(() => {
    server?.close();
  });

  it("returns parsed directory entries", async () => {
    ({ server, port } = await startMockServer((req) => {
      if (req.url.startsWith("/api/v1/fs/ls")) {
        return {
          body: {
            status: "ok",
            result: [
              { uri: "viking://memories/a.md", name: "a.md", isDir: false, size: 100, mode: 0o644, modTime: "2026-01-01", abstract: "file a" },
              { uri: "viking://memories/sub", name: "sub", isDir: true, size: 0, mode: 0o755, modTime: "2026-01-02", abstract: "" },
            ],
          },
        };
      }
      return { body: { status: "ok", result: null } };
    }));

    const client = new OVClient(baseConfig({ endpoint: `http://127.0.0.1:${port}` }));
    const entries = await client.ls("viking://memories/");
    expect(entries.length).toBe(2);
    expect(entries[0].name).toBe("a.md");
    expect(entries[0].isDir).toBe(false);
    expect(entries[1].name).toBe("sub");
    expect(entries[1].isDir).toBe(true);
  });

  it("returns empty array on non-array result", async () => {
    ({ server, port } = await startMockServer(() => {
      return { body: { status: "ok", result: { not: "an-array" } } };
    }));
    const client = new OVClient(baseConfig({ endpoint: `http://127.0.0.1:${port}` }));
    const entries = await client.ls("viking://test/");
    expect(entries).toEqual([]);
  });
});

describe("OVClient — readContent / abstract", () => {
  let server: Server;
  let port: number;

  afterEach(() => {
    server?.close();
  });

  it("readContent returns content string", async () => {
    ({ server, port } = await startMockServer((req) => {
      if (req.url.startsWith("/api/v1/content/read")) {
        return { body: { status: "ok", result: "full file content here" } };
      }
      return { body: { status: "ok", result: null } };
    }));
    const client = new OVClient(baseConfig({ endpoint: `http://127.0.0.1:${port}` }));
    const content = await client.readContent("viking://mem/test.md");
    expect(content).toBe("full file content here");
  });

  it("abstract returns abstract string", async () => {
    ({ server, port } = await startMockServer((req) => {
      if (req.url.startsWith("/api/v1/content/abstract")) {
        return { body: { status: "ok", result: "brief abstract" } };
      }
      return { body: { status: "ok", result: null } };
    }));
    const client = new OVClient(baseConfig({ endpoint: `http://127.0.0.1:${port}` }));
    const result = await client.abstract("viking://mem/test.md");
    expect(result).toBe("brief abstract");
  });

  it("returns null on error response", async () => {
    ({ server, port } = await startMockServer(() => {
      return { status: 404, body: { status: "error", error: { message: "not found" } } };
    }));
    const client = new OVClient(baseConfig({ endpoint: `http://127.0.0.1:${port}` }));
    const content = await client.readContent("viking://missing.md");
    expect(content).toBeNull();
  });
});
