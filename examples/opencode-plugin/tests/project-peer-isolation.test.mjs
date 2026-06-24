import test from "node:test"
import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { effectivePeerId, loadConfig } from "../lib/utils.mjs"

const originalEnv = {
  OPENVIKING_PLUGIN_CONFIG: process.env.OPENVIKING_PLUGIN_CONFIG,
  OPENVIKING_API_KEY: process.env.OPENVIKING_API_KEY,
  OPENVIKING_ACCOUNT: process.env.OPENVIKING_ACCOUNT,
  OPENVIKING_USER: process.env.OPENVIKING_USER,
  OPENVIKING_PEER_ID: process.env.OPENVIKING_PEER_ID,
  OPENVIKING_PEER_ID_OVERRIDE: process.env.OPENVIKING_PEER_ID_OVERRIDE,
}

function restoreEnv() {
  for (const [key, value] of Object.entries(originalEnv)) {
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }
}

function resetEnv(configPath) {
  for (const key of Object.keys(originalEnv)) delete process.env[key]
  process.env.OPENVIKING_PLUGIN_CONFIG = configPath
}

test("project peer isolation derives stable peer ids per OpenCode project", async () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "openviking-opencode-peer-"))
  const pluginRoot = path.join(tempRoot, "plugin")
  const configPath = path.join(tempRoot, "openviking-config.json")
  fs.mkdirSync(pluginRoot, { recursive: true })

  try {
    resetEnv(configPath)
    fs.writeFileSync(configPath, JSON.stringify({ peerId: "shared-peer", projectPeerIsolation: true }), "utf8")

    const alphaDir = path.join(tempRoot, "project-alpha")
    const betaDir = path.join(tempRoot, "project-beta")
    const alphaConfig = loadConfig(pluginRoot, alphaDir)
    const betaConfig = loadConfig(pluginRoot, betaDir)

    assert.notEqual(alphaConfig.peerId, betaConfig.peerId)
    assert.match(alphaConfig.peerId, /^shared-peer-project-alpha-[a-f0-9]{8}$/)
    assert.match(betaConfig.peerId, /^shared-peer-project-beta-[a-f0-9]{8}$/)
    assert.equal(loadConfig(pluginRoot, alphaDir).peerId, alphaConfig.peerId)
    assert.equal(effectivePeerId(alphaConfig), alphaConfig.peerId)

    fs.writeFileSync(configPath, JSON.stringify({ peerId: "shared-peer", projectPeerIsolation: false }), "utf8")
    assert.equal(loadConfig(pluginRoot, alphaDir).peerId, "shared-peer")
    assert.equal(loadConfig(pluginRoot, betaDir).peerId, "shared-peer")

    fs.writeFileSync(configPath, JSON.stringify({ peerId: "", projectPeerIsolation: true }), "utf8")
    assert.match(loadConfig(pluginRoot, alphaDir).peerId, /^opencode-project-alpha-[a-f0-9]{8}$/)

    fs.writeFileSync(configPath, JSON.stringify({ peerId: "file-peer", projectPeerIsolation: true }), "utf8")
    process.env.OPENVIKING_PEER_ID = "env-peer"
    assert.match(loadConfig(pluginRoot, alphaDir).peerId, /^env-peer-project-alpha-[a-f0-9]{8}$/)

    process.env.OPENVIKING_PEER_ID_OVERRIDE = "override-peer"
    assert.equal(loadConfig(pluginRoot, alphaDir).peerId, "override-peer")
    assert.equal(loadConfig(pluginRoot, betaDir).peerId, "override-peer")

    delete process.env.OPENVIKING_PEER_ID
    delete process.env.OPENVIKING_PEER_ID_OVERRIDE
    assert.equal(loadConfig(pluginRoot, undefined).peerId, "file-peer")
  } finally {
    restoreEnv()
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})
