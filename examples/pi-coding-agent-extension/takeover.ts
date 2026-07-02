/**
 * OV Context Takeover — OpenViking becomes the authoritative long-term
 * context store for pi, vikingbot-style, implemented through pi's `context`
 * hook. See TAKEOVER.md for the design.
 *
 * Responsibilities:
 * - Track how much of the conversation is covered by the OV archive
 *   (`coveredUserTurns`) and cache the archive overview text.
 * - `transformContext()`: on every LLM call, drop covered messages and
 *   inject the overview as a synthetic user message. Byte-stable between
 *   commits so the prompt cache stays warm.
 * - Trigger OV commits when synced-token pressure crosses the threshold
 *   (vikingbot's commit_token_threshold), then advance the boundary.
 * - Replace pi's compaction summary with OV's overview when pi compaction
 *   fires anyway (`session_before_compact`).
 * - Persist/restore state via pi custom session entries ("ov-takeover").
 */
import type { OVClient } from "./client.js";
import type { OVConfig } from "./config.js";
import type { SyncManager } from "./sync.js";
import { estimateTokens } from "./sync.js";

export const TAKEOVER_ENTRY_TYPE = "ov-takeover";
export const OVERVIEW_MARKER = "[OpenViking Session Context]";

export interface TakeoverPersistedState {
  coveredUserTurns: number;
  overview: string;
  /** Carried across processes so `pi -p`/`pi -c` runs accumulate toward the threshold. */
  pendingTokens: number;
}

/** Minimal structural view of pi AgentMessages — keep `any`-free where it matters. */
interface MsgLike {
  role: string;
  content?: unknown;
  timestamp?: number;
}

/** Stable-ish fingerprint of a message: role + flattened text prefix. */
export function fingerprintMessage(msg: MsgLike): string {
  const text = flattenContent(msg);
  return `${msg.role}:${text.length}:${text.slice(0, 200)}`;
}

export function flattenContent(msg: MsgLike): string {
  const c = (msg as any).content;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) {
    return c
      .filter((b: any) => b && b.type === "text" && typeof b.text === "string")
      .map((b: any) => b.text)
      .join("");
  }
  return "";
}

/** Is this a real user turn start (not our own injected overview)? */
export function isUserTurnStart(msg: MsgLike): boolean {
  if (msg.role !== "user") return false;
  return !flattenContent(msg).startsWith(OVERVIEW_MARKER);
}

/** Count real user turn starts in a message list. */
export function countUserTurns(messages: MsgLike[]): number {
  let n = 0;
  for (const m of messages) if (isUserTurnStart(m)) n++;
  return n;
}

/**
 * Index of the first message of the (n+1)-th user turn, i.e. the first kept
 * message when the first `n` user turns are covered by the OV archive.
 * Returns -1 when the list has fewer than n+1 user turns.
 */
export function findBoundaryIndex(messages: MsgLike[], coveredUserTurns: number): number {
  let seen = 0;
  for (let i = 0; i < messages.length; i++) {
    if (isUserTurnStart(messages[i])) {
      seen++;
      if (seen === coveredUserTurns + 1) return i;
    }
  }
  return -1;
}

export class TakeoverManager {
  private client: OVClient;
  private sync: SyncManager;
  private config: OVConfig;
  private appendEntry: (customType: string, data?: unknown) => void;
  private log: (msg: string) => void;

  // State
  private coveredUserTurns = 0;
  private overview = "";
  private fingerprint: string | null = null; // of last covered message; lazily materialized
  private lastSeenUserTurns = 0;
  private pendingTokens = 0;
  private committing = false;

  constructor(opts: {
    client: OVClient;
    sync: SyncManager;
    config: OVConfig;
    appendEntry: (customType: string, data?: unknown) => void;
    log?: (msg: string) => void;
  }) {
    this.client = opts.client;
    this.sync = opts.sync;
    this.config = opts.config;
    this.appendEntry = opts.appendEntry;
    this.log = opts.log ?? (() => {});
  }

  get enabled(): boolean {
    return this.config.takeoverEnabled;
  }

  get state(): TakeoverPersistedState & { pendingTokens: number; lastSeenUserTurns: number } {
    return {
      coveredUserTurns: this.coveredUserTurns,
      overview: this.overview,
      pendingTokens: this.pendingTokens,
      lastSeenUserTurns: this.lastSeenUserTurns,
    };
  }

  /** Restore persisted state from session entries (session_start / resume). */
  restore(entries: Array<{ type?: string; customType?: string; data?: unknown }>): void {
    for (let i = entries.length - 1; i >= 0; i--) {
      const e = entries[i];
      if (e?.type === "custom" && e.customType === TAKEOVER_ENTRY_TYPE && e.data) {
        const d = e.data as Partial<TakeoverPersistedState>;
        if (typeof d.coveredUserTurns === "number" && d.coveredUserTurns >= 0) {
          this.coveredUserTurns = d.coveredUserTurns;
          this.overview = typeof d.overview === "string" ? d.overview : "";
          this.pendingTokens = typeof d.pendingTokens === "number" && d.pendingTokens > 0 ? d.pendingTokens : 0;
          this.fingerprint = null; // re-materialize on next context call
          this.log(`takeover: restored boundary at ${this.coveredUserTurns} user turns, ${this.pendingTokens} pending tokens`);
        }
        return;
      }
    }
  }

  /**
   * The context hook body. Called with pi's deep-copied AgentMessage list on
   * every LLM call, BEFORE recall injection. Returns the transformed list.
   */
  transformContext(messages: MsgLike[]): MsgLike[] {
    this.lastSeenUserTurns = countUserTurns(messages);

    if (!this.enabled) return messages;
    if (this.coveredUserTurns <= 0 || !this.overview) return messages;

    const boundaryIdx = findBoundaryIndex(messages, this.coveredUserTurns);
    if (boundaryIdx <= 0) {
      // Branch switched / compaction rewrote history — fewer turns than we
      // covered. Reset; full history until the next commit.
      this.resetBoundary("history shorter than boundary");
      return messages;
    }

    const lastCovered = messages[boundaryIdx - 1];
    const fp = fingerprintMessage(lastCovered);
    if (this.fingerprint === null) {
      this.fingerprint = fp; // materialize after commit/restore
    } else if (this.fingerprint !== fp) {
      this.resetBoundary("fingerprint mismatch (branch navigation?)");
      return messages;
    }

    const kept = messages.slice(boundaryIdx);
    const overviewMsg: MsgLike = {
      role: "user",
      content:
        `${OVERVIEW_MARKER} Earlier conversation was archived to OpenViking and summarized below. ` +
        `Use viking_search / viking_archive_expand for details.\n\n${this.truncatedOverview()}`,
      // Deterministic: derive from the first kept message so repeated calls
      // between commits produce byte-identical context (prompt cache).
      timestamp: typeof kept[0]?.timestamp === "number" ? kept[0].timestamp! - 1 : 0,
    };
    return [overviewMsg, ...kept];
  }

  /** Called after each synced turn with the estimated token count. */
  async onTurnSynced(estTokens: number): Promise<void> {
    if (!this.enabled) return;
    this.pendingTokens += Math.max(0, estTokens);
    if (this.pendingTokens < this.config.takeoverTokenThreshold) return;
    if (this.lastSeenUserTurns <= this.config.takeoverKeepRecentTurns) return;
    await this.commitAndAdvance();
  }

  /**
   * Commit the OV session and advance the boundary. Serialized; concurrent
   * calls no-op. Keeps serving the previous overview until the new one lands.
   */
  async commitAndAdvance(): Promise<boolean> {
    if (this.committing) return false;
    this.committing = true;
    try {
      await this.sync.flushQueue();
      const archiveUri = await this.sync.commit(false);
      if (!archiveUri) {
        this.log("takeover: commit failed; retaining pending tokens");
        return false;
      }

      const overview = await this.pollOverview();
      if (!overview) {
        // Commit accepted but overview not ready: don't advance the boundary —
        // never inject an empty overview. Next threshold crossing retries.
        this.log("takeover: overview not ready; boundary unchanged");
        this.pendingTokens = 0;
        return false;
      }

      const newCovered = Math.max(
        0,
        this.lastSeenUserTurns - this.config.takeoverKeepRecentTurns,
      );
      if (newCovered > this.coveredUserTurns) {
        this.coveredUserTurns = newCovered;
        this.fingerprint = null; // re-materialize on next context call
      }
      this.overview = overview;
      this.pendingTokens = 0;
      this.persist();
      this.log(`takeover: boundary advanced to ${this.coveredUserTurns} user turns`);
      return true;
    } finally {
      this.committing = false;
    }
  }

  /**
   * session_before_compact handler body. Returns a pi CompactionResult-shaped
   * object (summary from OV) or undefined to fall back to pi's compaction.
   */
  async handleBeforeCompact(preparation: {
    firstKeptEntryId: string | undefined;
    tokensBefore: number;
  }): Promise<
    | { compaction: { summary: string; firstKeptEntryId: string; tokensBefore: number; details: { source: string } } }
    | undefined
  > {
    if (!this.enabled) return undefined;
    if (!preparation.firstKeptEntryId) return undefined; // nothing tool-safe to cut at

    await this.sync.flushQueue();
    const archiveUri = await this.sync.commit(false);
    if (!archiveUri) return undefined; // fail-open: pi's compaction proceeds

    const overview = await this.pollOverview();
    if (!overview) return undefined;

    // Pi's CompactionEntry now covers the pre-cut span — our own boundary
    // must reset, otherwise we'd double-drop.
    this.overview = overview;
    this.resetBoundary("pi compaction absorbed boundary");
    this.pendingTokens = 0;
    this.persist();

    return {
      compaction: {
        summary: `${OVERVIEW_MARKER}\n${this.truncatedOverview()}`,
        firstKeptEntryId: preparation.firstKeptEntryId!,
        tokensBefore: preparation.tokensBefore,
        details: { source: "openviking" },
      },
    };
  }

  /** Final flush + state persist (session_shutdown). */
  async shutdown(): Promise<void> {
    if (!this.enabled) return;
    this.persist();
  }

  // --- internals ---

  private resetBoundary(reason: string): void {
    if (this.coveredUserTurns !== 0 || this.fingerprint !== null) {
      this.log(`takeover: boundary reset (${reason})`);
    }
    this.coveredUserTurns = 0;
    this.fingerprint = null;
  }

  private truncatedOverview(): string {
    const budget = this.config.takeoverOverviewBudget;
    if (estimateTokens(this.overview) <= budget) return this.overview;
    return this.overview.slice(0, budget * 3) + "\n…(truncated)";
  }

  private persist(): void {
    try {
      this.appendEntry(TAKEOVER_ENTRY_TYPE, {
        coveredUserTurns: this.coveredUserTurns,
        overview: this.overview,
        pendingTokens: this.pendingTokens,
      } satisfies TakeoverPersistedState);
    } catch {
      // Best effort — resume simply starts with full history.
    }
  }

  private async pollOverview(): Promise<string> {
    const sessionId = this.sync.sessionId;
    if (!sessionId) return "";
    for (let i = 0; i < this.config.takeoverOverviewPollMax; i++) {
      const ctx = await this.client.getSessionContext(
        sessionId,
        this.config.takeoverOverviewBudget * 4,
      );
      const overview = ctx?.latest_archive_overview?.trim();
      if (overview) return overview;
      await new Promise((r) => setTimeout(r, this.config.takeoverOverviewPollMs));
    }
    return "";
  }
}
