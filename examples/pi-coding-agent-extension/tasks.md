# pi-extension — OV bot based on PI: task log & decisions

Working branch: `worktree-pibot` (worktree off `main`).
Date started: 2026-07-03. All major decisions confirmed by Zayn 2026-07-03.

## Decisions (confirmed by Zayn)

1. **Deliverable**: production-grade PI extension package — "OV bot based on PI" = pi + this package (analogous to vikingbot = nanobot architecture + OV). Not a channel daemon.
2. **Takeover depth**: reference vikingbot's `session_context_enabled` architecture, implemented through **PI's `context` hook** ("PI provides context hook. use that. so that layer of deep"):
   - turns append to the OV session incrementally (write queue), commits at token threshold / pre-compact / shutdown;
   - after a commit, a **commit boundary** is tracked (persisted via pi custom entry so resume restores it); the `context` hook drops messages before the boundary and injects OV's `latest_archive_overview` instead — the hook plays the role of vikingbot's post-commit `session.clear()`;
   - pi's own LLM summarizer must not own long-term context: `session_before_compact` commits to OV and supplies the OV overview as the compaction summary (or cancels, pending token-accounting findings from the 0.80.3 study);
   - hook output stable between commits (prompt cache); never split tool-call/result pairs; boundary lands only at turn edges.
3. **Location**: keep in `examples/`. Merge into `examples/pi-coding-agent-extension` if the result is very similar; otherwise new `examples/pi-extension`. Final call after the pi-0.80.3 breaking-change report (old example targets renamed-away `@mariozechner` 0.73 packages).
4. **Sequencing**: baseline vikingbot suite first (fix nothing unless we broke it), then build the PI extension and run its tests.
5. **Test strategy**: **live e2e is the acceptance gate** — real pi + extension against https://ov.zaynjarvis.com (user API key) and super-relay (`model_api/experimental_0630`). A mocked-OV unit suite kept for CI determinism.

## Assumptions (unattended; flag if wrong)

6. Pi target: `@earendil-works/pi-coding-agent` ^0.80.3. TypeScript, zero runtime deps, vitest, Node >= 20.
7. Because OV context replaces committed history only via the *overview* (pi's session file still holds recent full-fidelity messages), OV-side capture remains extraction-oriented (filtered, stripped) as in the existing example — capture fidelity is NOT required for history reconstruction.
8. Commit on the worktree branch at the end; no push without Zayn.

## Task list

- [x] Baseline: vikingbot test suite run — **181 passed, 0 failed** (venv `.venv-pibot`, extras `[test,bot]`; note: root pyproject has no `dev`-with-pytest or `vikingbot` extras — CI yml in bot/.github is stale from the standalone repo)
- [x] Study pi 0.80.3 extension API — only hard break vs the 0.73 example: package scope renames (`@mariozechner/*` → `@earendil-works/*`); all used events source-compatible → merge-in-place decision
- [x] Verified live endpoints: OV health ok (account Zayn, user pi, api_key mode); session create→message→commit→`latest_archive_overview` cycle works (~20s async commit); super-relay chat ok (returns `reasoning_content`)
- [x] Record decisions/assumptions (this file)
- [x] DESIGN — TAKEOVER.md (user-turn boundary via `context` hook, OV summary via `session_before_compact`)
- [x] Migrate to @earendil-works 0.80.3: imports, package.json (pi-ai + typebox pinned directly — pi ships an npm-shrinkwrap so its deps nest), tsconfig; fixed 5 pre-existing type errors → `tsc --noEmit` green
- [x] Implement takeover layer: takeover.ts (boundary + fingerprint + overview injection + threshold commits + OV-summary compaction + entry persistence incl. pendingTokens across `pi -p`/`-c` processes), sync.ts faithful mode + flushQueue, config takeover block, index.ts wiring with idempotent start() (session_start skipped on `pi -c`)
- [x] pi + super-relay headless smoke (`pi -p`, custom provider models.json) → SMOKE-OK
- [x] Unit tests — **144 passed / 5 files** (takeover 58, sync 48, client 21, index-wiring 9, config 8), `tsc --noEmit` clean
- [x] Live e2e (scripts/e2e-live.sh) — **ALL 10 CHECKS PASSED** against ov.zaynjarvis.com + super-relay: state restored across `pi -p`/`-c` processes; threshold commit → OV archive → boundary advance; overview injected and archived turn dropped from real provider payload; model recovered the archived fact from the overview; recall + viking_* tools fired live; throwaway session cleaned up

## Real bugs found by testing (fixed)

1. **turn_end capture never fired in one-shot runs** — pi's `turnIndex` counts LLM rounds within a run and resets to 0 per prompt; the example's `turnIndex <= syncedTurnCount` dedup skipped round 0, so `pi -p` synced nothing. Capture moved to `agent_end` (one event per run, carries the run's new messages).
2. **`pi -c` continuations killed the extension** — OV rejects duplicate session creation with `ALREADY_EXISTS`; `ensureSession` treated that as failure → `connected=false` for every resumed process. `createSession` now treats ALREADY_EXISTS as success.
3. **WriteQueue dropped queued turns after a failed write** — only the failed turn was re-queued; the rest of the spliced batch was lost. Now re-queues the failed + remaining batch.
4. (pre-existing, minor) 5 type errors under strict tsc; `sync.commit` passed a nonexistent `wait` param and mis-typed the commit result.
- [x] Docs: README rewritten for takeover + 0.80.3 + dev/testing; TAKEOVER.md design; this task log
- [x] Commit on `worktree-pibot` branch (no push without Zayn)
