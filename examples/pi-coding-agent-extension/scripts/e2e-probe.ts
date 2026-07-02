/**
 * E2E probe extension — records what pi actually sends to the LLM provider.
 *
 * Loaded alongside the OV extension in live e2e runs (`-e scripts/e2e-probe.ts`).
 * Writes each provider request payload plus the pi session id into
 * $OV_E2E_OUT so the driver (e2e-live.mjs) can assert on real request content.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

export default function (pi: ExtensionAPI) {
  const outDir = process.env.OV_E2E_OUT;
  if (!outDir) return;
  mkdirSync(outDir, { recursive: true });

  const turn = process.env.OV_E2E_TURN ?? "0";
  let n = 0;

  const writeSessionId = (ctx: { sessionManager: { getSessionId(): string } }) => {
    try {
      writeFileSync(join(outDir, "session-id.txt"), ctx.sessionManager.getSessionId());
    } catch { /* best effort */ }
  };

  pi.on("session_start", async (_event, ctx) => writeSessionId(ctx));
  pi.on("before_agent_start", async (_event, ctx) => writeSessionId(ctx));

  pi.on("before_provider_request", async (event, _ctx) => {
    n++;
    try {
      writeFileSync(
        join(outDir, `payload-t${turn}-${String(n).padStart(2, "0")}.json`),
        JSON.stringify(event.payload, null, 2),
      );
    } catch { /* best effort */ }
  });
}
