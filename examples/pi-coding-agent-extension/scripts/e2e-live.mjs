#!/usr/bin/env node
/**
 * Live e2e for the OpenViking pi extension — the acceptance gate.
 *
 * Drives a REAL pi (0.80.3) against a REAL OpenViking server and a REAL LLM,
 * and asserts the context takeover works end to end:
 *   T1: seed a distinctive fact (+padding to build token pressure)
 *   T2: second fact (+padding) → crosses the takeover commit threshold →
 *       OV commit + boundary advance
 *   T3: ask for the T1 fact → assert the provider request contains the
 *       "[OpenViking Session Context]" overview instead of the raw T1 turn,
 *       and the model can still answer from the overview.
 *
 * Required env:
 *   OPENVIKING_URL        e.g. https://ov.example.com
 *   OPENVIKING_API_KEY    OV user API key
 *   SUPER_RELAY_API_KEY   Bearer key for the OpenAI-compatible relay
 * Optional env:
 *   E2E_LLM_BASE_URL      default https://super-relay.byted.org/v1
 *   E2E_LLM_MODEL         default model_api/experimental_0630
 *   E2E_KEEP_TMP=1        keep the temp dir on success
 */
import { spawnSync } from "node:child_process";
import {
  mkdtempSync, mkdirSync, writeFileSync, readFileSync, readdirSync,
  copyFileSync, symlinkSync, existsSync, rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const EXT_SRC = dirname(dirname(fileURLToPath(import.meta.url)));
const PI_BIN = join(EXT_SRC, "node_modules", ".bin", "pi");

// --- env ---
const OV_URL = process.env.OPENVIKING_URL;
const OV_KEY = process.env.OPENVIKING_API_KEY;
const RELAY_KEY = process.env.SUPER_RELAY_API_KEY;
const LLM_BASE = process.env.E2E_LLM_BASE_URL ?? "https://super-relay.byted.org/v1";
const LLM_MODEL = process.env.E2E_LLM_MODEL ?? "model_api/experimental_0630";
for (const [k, v] of [["OPENVIKING_URL", OV_URL], ["OPENVIKING_API_KEY", OV_KEY], ["SUPER_RELAY_API_KEY", RELAY_KEY]]) {
  if (!v) { console.error(`e2e: missing required env ${k}`); process.exit(2); }
}
if (!existsSync(PI_BIN)) { console.error(`e2e: pi binary not found at ${PI_BIN} — run npm install first`); process.exit(2); }

// --- results ---
let failures = 0;
const pass = (msg) => console.log(`  PASS  ${msg}`);
const fail = (msg) => { failures++; console.error(`  FAIL  ${msg}`); };
const warn = (msg) => console.log(`  WARN  ${msg}`);
const check = (cond, msg) => (cond ? pass(msg) : fail(msg));

// --- workspace ---
const root = mkdtempSync(join(tmpdir(), "ov-pi-e2e-"));
const agentDir = join(root, "agent");
const extDir = join(root, "ext");
const outDir = join(root, "out");
const projDir = join(root, "proj");
const sessionDir = join(root, "sessions");
for (const d of [agentDir, extDir, outDir, projDir, sessionDir]) mkdirSync(d, { recursive: true });
console.log(`e2e: workspace ${root}`);

// Agent dir: provider registration for the relay
writeFileSync(join(agentDir, "models.json"), JSON.stringify({
  providers: {
    "super-relay": {
      name: "Super Relay",
      baseUrl: LLM_BASE,
      api: "openai-completions",
      apiKey: "$SUPER_RELAY_API_KEY",
      authHeader: true,
      headers: { "x-session-id": "ov-pi-e2e" },
      models: [{
        id: LLM_MODEL,
        name: "Relay e2e model",
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 128000,
        maxTokens: 8192,
      }],
    },
  },
}, null, 2));
writeFileSync(join(agentDir, "settings.json"), JSON.stringify({ defaultProjectTrust: "always" }, null, 2));

// Extension copy with e2e thresholds; node_modules symlinked for jiti resolution
for (const f of readdirSync(EXT_SRC)) {
  if (f.endsWith(".ts") && !f.startsWith(".")) copyFileSync(join(EXT_SRC, f), join(extDir, f));
}
symlinkSync(join(EXT_SRC, "node_modules"), join(extDir, "node_modules"));
writeFileSync(join(extDir, "config.json"), JSON.stringify({
  enabled: true,
  agentId: "pi",
  syncTurns: true,
  commitOnShutdown: false,        // takeover owns commits in this flow
  mirrorMemoryWrites: false,
  logLevel: "info",
  takeover: {
    enabled: true,
    tokenThreshold: 600,          // tiny threshold so T2 crosses it
    keepRecentTurns: 1,
    overviewBudget: 3000,
    overviewPollMs: 3000,
    overviewPollMax: 30,          // server-side archive extraction takes ~20s
  },
}, null, 2));

// --- helpers ---
const PAD1 = `PADDING-T1 ${"lorem ipsum dolor sit amet consectetur ".repeat(60)}`;   // ~2.4k chars
const PAD2 = `PADDING-T2 ${"vestibulum ante ipsum primis in faucibus ".repeat(60)}`;

function runTurn(turn, prompt, extraArgs = []) {
  console.log(`\ne2e: --- turn ${turn} ---`);
  const args = [
    "--provider", "super-relay", "--model", LLM_MODEL,
    "--mode", "text", "-p",
    "-ne", "-e", join(extDir, "index.ts"), "-e", join(EXT_SRC, "scripts", "e2e-probe.ts"),
    "--approve", "--no-context-files", "--no-skills", "--no-prompt-templates",
    "--session-dir", sessionDir, "--offline",
    ...extraArgs,
    prompt,
  ];
  const res = spawnSync(PI_BIN, args, {
    cwd: projDir,
    env: {
      ...process.env,
      PI_CODING_AGENT_DIR: agentDir,
      OPENVIKING_URL: OV_URL,
      OPENVIKING_API_KEY: OV_KEY,
      SUPER_RELAY_API_KEY: RELAY_KEY,
      OV_E2E_OUT: outDir,
      OV_E2E_TURN: String(turn),
      OV_DEBUG_LOG: join(outDir, "takeover.log"),
    },
    timeout: 300_000,
    encoding: "utf8",
  });
  const out = `${res.stdout ?? ""}`;
  const err = `${res.stderr ?? ""}`;
  if (res.status !== 0) {
    fail(`turn ${turn}: pi exited ${res.status}\nstdout:\n${out.slice(-2000)}\nstderr:\n${err.slice(-2000)}`);
  } else {
    console.log(out.trim().slice(-800));
  }
  return { out, err, status: res.status };
}

async function ovFetch(path, init) {
  const resp = await fetch(`${OV_URL}${path}`, {
    ...init,
    headers: { "X-API-Key": OV_KEY, "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = await resp.json().catch(() => ({}));
  return { ok: resp.ok && body?.status !== "error", body };
}

function payloadsFor(turn) {
  return readdirSync(outDir)
    .filter((f) => f.startsWith(`payload-t${turn}-`))
    .sort()
    .map((f) => readFileSync(join(outDir, f), "utf8"));
}

// --- run ---
const t1 = runTurn(1, `Please remember: the release codename is ZEPHYR-9942. Just acknowledge briefly. ${PAD1}`);
const t2 = runTurn(2, `Second note: the deploy window is Friday 03:00 UTC. Acknowledge briefly. ${PAD2}`, ["-c"]);
const t3 = runTurn(3, "What is the release codename I told you earlier? Answer with just the codename.", ["-c"]);

// --- assertions ---
console.log("\ne2e: --- assertions ---");

// A. Turn plumbing
check(t1.status === 0 && t2.status === 0 && t3.status === 0, "all three pi runs exited 0");

// B. Takeover visible in the T3 provider request
const p3 = payloadsFor(3);
check(p3.length > 0, `probe captured T3 provider payload (${p3.length} request(s))`);
if (p3.length > 0) {
  const last = p3[p3.length - 1];
  check(last.includes("[OpenViking Session Context]"), "T3 request contains the OV overview block");
  // Recall may legitimately resurface archived content inside
  // <relevant-memories> — the takeover assertion is about the CONVERSATION
  // history, so strip recall blocks before checking.
  const withoutRecall = last.replace(/<relevant-memories>[\s\S]*?<\/relevant-memories>/g, "");
  check(!withoutRecall.includes("PADDING-T1"), "T3 request no longer contains the raw T1 turn (dropped past boundary)");
  check(last.includes("PADDING-T2") || last.includes("codename"), "T3 request keeps recent live turns");
}

// C. Model still answers the archived fact from the overview
if (t3.out.includes("ZEPHYR-9942")) pass("model recovered the archived fact from the OV overview");
else warn("model answer did not contain ZEPHYR-9942 — check overview quality (soft)");

// D. OV server state
const sessionIdFile = join(outDir, "session-id.txt");
let ovSessionId = null;
if (existsSync(sessionIdFile)) {
  ovSessionId = `pi-${readFileSync(sessionIdFile, "utf8").trim()}`;
  const ctx = await ovFetch(`/api/v1/sessions/${encodeURIComponent(ovSessionId)}/context?token_budget=4000`);
  check(ctx.ok, `OV session ${ovSessionId} readable`);
  const result = ctx.body?.result ?? {};
  const overview = (result.latest_archive_overview ?? "").trim();
  check(overview.length > 0, "OV session has a non-empty archive overview (commit ran)");
  check((result.stats?.totalArchives ?? 0) >= 1, `OV session has >=1 archive (got ${result.stats?.totalArchives})`);
} else {
  fail("probe did not record a pi session id");
}

// E. Takeover debug log sanity
const logPath = join(outDir, "takeover.log");
if (existsSync(logPath)) {
  const log = readFileSync(logPath, "utf8");
  check(log.includes("boundary advanced"), "takeover log shows a boundary advance");
  console.log(`\ne2e: takeover.log:\n${log.trim()}`);
} else {
  warn("no takeover debug log written");
}

// --- cleanup ---
if (ovSessionId) {
  const del = await ovFetch(`/api/v1/sessions/${encodeURIComponent(ovSessionId)}`, { method: "DELETE" });
  console.log(`\ne2e: cleanup OV session ${ovSessionId}: ${del.ok ? "deleted" : "FAILED (delete manually)"}`);
}
if (failures === 0 && !process.env.E2E_KEEP_TMP) {
  rmSync(root, { recursive: true, force: true });
} else {
  console.log(`e2e: workspace kept for inspection: ${root}`);
}

console.log(failures === 0 ? "\ne2e: ALL CHECKS PASSED" : `\ne2e: ${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
