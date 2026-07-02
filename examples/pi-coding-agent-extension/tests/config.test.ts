import { describe, it, expect } from "vitest";
import { loadConfig } from "../config.js";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

function makeTempDir(): string {
  return mkdtempSync(join(tmpdir(), "ov-config-test-"));
}

describe("loadConfig", () => {
  it("returns defaults when no config.json in dir", () => {
    const dir = makeTempDir();
    try {
      const cfg = loadConfig(dir);
      expect(cfg.enabled).toBe(true);
      expect(cfg.endpoint).toBe("http://127.0.0.1:1933");
      expect(cfg.apiKey).toBe("");
      expect(cfg.takeoverEnabled).toBe(true);
      expect(cfg.takeoverTokenThreshold).toBe(30000);
      expect(cfg.takeoverKeepRecentTurns).toBe(3);
      expect(cfg.takeoverOverviewBudget).toBe(3000);
      expect(cfg.takeoverOverviewPollMs).toBe(2000);
      expect(cfg.takeoverOverviewPollMax).toBe(15);
      expect(cfg.recallBudget).toBe(2000);
      expect(cfg.commitTokenThreshold).toBe(20000);
      expect(cfg.captureMode).toBe("semantic");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("nested takeover block maps to flat takeover* fields", () => {
    const dir = makeTempDir();
    try {
      writeFileSync(join(dir, "config.json"), JSON.stringify({
        takeover: {
          enabled: true,
          tokenThreshold: 50000,
          keepRecentTurns: 5,
          overviewBudget: 4000,
          overviewPollMs: 1000,
          overviewPollMax: 10,
        },
      }));
      const cfg = loadConfig(dir);
      expect(cfg.takeoverEnabled).toBe(true);
      expect(cfg.takeoverTokenThreshold).toBe(50000);
      expect(cfg.takeoverKeepRecentTurns).toBe(5);
      expect(cfg.takeoverOverviewBudget).toBe(4000);
      expect(cfg.takeoverOverviewPollMs).toBe(1000);
      expect(cfg.takeoverOverviewPollMax).toBe(10);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("partial takeover block keeps other defaults", () => {
    const dir = makeTempDir();
    try {
      writeFileSync(join(dir, "config.json"), JSON.stringify({
        takeover: {
          tokenThreshold: 99999,
        },
      }));
      const cfg = loadConfig(dir);
      expect(cfg.takeoverTokenThreshold).toBe(99999);
      // Other takeover fields retain defaults
      expect(cfg.takeoverEnabled).toBe(true);
      expect(cfg.takeoverKeepRecentTurns).toBe(3);
      expect(cfg.takeoverOverviewBudget).toBe(3000);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("malformed JSON → defaults", () => {
    const dir = makeTempDir();
    try {
      writeFileSync(join(dir, "config.json"), "{ this is not valid json!!!");
      const cfg = loadConfig(dir);
      expect(cfg.enabled).toBe(true);
      expect(cfg.takeoverTokenThreshold).toBe(30000);
      expect(cfg.endpoint).toBe("http://127.0.0.1:1933");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("top-level fields still merge with defaults", () => {
    const dir = makeTempDir();
    try {
      writeFileSync(join(dir, "config.json"), JSON.stringify({
        endpoint: "https://ov.example.com",
        apiKey: "test-key",
        recallBudget: 5000,
        captureMode: "keyword",
      }));
      const cfg = loadConfig(dir);
      expect(cfg.endpoint).toBe("https://ov.example.com");
      expect(cfg.apiKey).toBe("test-key");
      expect(cfg.recallBudget).toBe(5000);
      expect(cfg.captureMode).toBe("keyword");
      // Unspecified fields retain defaults
      expect(cfg.takeoverEnabled).toBe(true);
      expect(cfg.takeoverTokenThreshold).toBe(30000);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("top-level + nested takeover both merge", () => {
    const dir = makeTempDir();
    try {
      writeFileSync(join(dir, "config.json"), JSON.stringify({
        endpoint: "https://custom.ov.com",
        logLevel: "info",
        takeover: {
          enabled: false,
          keepRecentTurns: 1,
        },
      }));
      const cfg = loadConfig(dir);
      expect(cfg.endpoint).toBe("https://custom.ov.com");
      expect(cfg.logLevel).toBe("info");
      expect(cfg.takeoverEnabled).toBe(false);
      expect(cfg.takeoverKeepRecentTurns).toBe(1);
      // Defaults still present
      expect(cfg.takeoverTokenThreshold).toBe(30000);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("empty JSON object → all defaults", () => {
    const dir = makeTempDir();
    try {
      writeFileSync(join(dir, "config.json"), "{}");
      const cfg = loadConfig(dir);
      expect(cfg.takeoverTokenThreshold).toBe(30000);
      expect(cfg.endpoint).toBe("http://127.0.0.1:1933");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("takeover block with wrong types is ignored for those fields", () => {
    const dir = makeTempDir();
    try {
      writeFileSync(join(dir, "config.json"), JSON.stringify({
        takeover: {
          enabled: "not-a-boolean",
          tokenThreshold: "also-not-a-number",
        },
      }));
      const cfg = loadConfig(dir);
      // Wrong types → defaults kept
      expect(cfg.takeoverEnabled).toBe(true);
      expect(cfg.takeoverTokenThreshold).toBe(30000);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
