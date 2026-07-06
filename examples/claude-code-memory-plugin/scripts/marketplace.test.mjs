import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptsDir = dirname(fileURLToPath(import.meta.url));
const pluginDir = resolve(scriptsDir, "..");
const repoRoot = resolve(scriptsDir, "..", "..", "..");
const rootCatalogPath = join(repoRoot, ".claude-plugin", "marketplace.json");
const localCatalogPath = join(repoRoot, "examples", ".claude-plugin", "marketplace.json");
const manifestPath = join(pluginDir, ".claude-plugin", "plugin.json");

const PLUGIN_NAME = "openviking-memory";

function readJson(path) {
  return JSON.parse(readFileSync(path, "utf-8"));
}

test("repo-root Claude marketplace catalog uses git-subdir source", () => {
  assert.ok(existsSync(rootCatalogPath), `missing catalog at ${rootCatalogPath}`);
  const catalog = readJson(rootCatalogPath);
  assert.equal(catalog.name, "openviking");
  const entry = catalog.plugins?.find((p) => p?.name === PLUGIN_NAME);
  assert.ok(entry, `root catalog must contain ${PLUGIN_NAME}`);
  assert.deepEqual(entry.source, {
    type: "git-subdir",
    url: "https://github.com/volcengine/OpenViking.git",
    path: "examples/claude-code-memory-plugin",
    ref: "main",
  });
});

test("local Claude marketplace entry name matches plugin manifest", () => {
  const catalog = readJson(localCatalogPath);
  const manifest = readJson(manifestPath);
  const entry = catalog.plugins?.find((p) => p?.name === PLUGIN_NAME);
  assert.ok(entry, `local catalog must contain ${PLUGIN_NAME}`);
  assert.equal(entry.name, manifest.name);
  assert.equal(entry.source, "./claude-code-memory-plugin");
});

test("Claude .mcp.json starts the stdio MCP proxy", () => {
  const mcp = readJson(join(pluginDir, ".mcp.json"));
  const server = mcp.openviking;
  assert.ok(server, ".mcp.json must define openviking server");
  assert.equal(server.command, "node");
  assert.deepEqual(server.args, ["${CLAUDE_PLUGIN_ROOT}/servers/mcp-proxy.mjs"]);
  assert.ok(!("type" in server), ".mcp.json should not keep HTTP MCP type");
  assert.ok(!("url" in server), ".mcp.json should not keep direct HTTP url");
  execFileSync("node", ["--check", join(pluginDir, "servers", "mcp-proxy.mjs")], { stdio: "pipe" });
});
