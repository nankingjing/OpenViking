import type { OVClient } from "./client.js";
import type { OVConfig } from "./config.js";

// --- Memory Stripping ---

/**
 * Strip all injected/synthetic blocks before syncing to OV.
 * Prevents feedback loop where OV indexes injected context as conversation.
 */
export function stripInjectedBlocks(text: string): string {
  // 1. <relevant-memories>...</relevant-memories>
  text = text.replace(/<relevant-memories>[\s\S]*?<\/relevant-memories>/g, "");
  // 2. <system-reminder>...</system-reminder>
  text = text.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, "");
  // 3. <openviking-context>...</openviking-context>
  text = text.replace(/<openviking-context[\s\S]*?<\/openviking-context>/g, "");
  // 4. [Subagent Context]... (until double newline or end)
  text = text.replace(/\[Subagent Context\][\s\S]*?(?=\n\n|$)/g, "");
  // 5. Null bytes
  text = text.replace(/\x00/g, "");
  return text.trim();
}

// --- CJK-aware Token Estimation ---

export function estimateTokens(text: string): number {
  if (!text) return 0;
  let cjk = 0;
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) >= 0x3000) cjk++;
  }
  const other = text.length - cjk;
  return Math.ceil(cjk * 1.5 + other / 4);
}

// --- Capture Filtering ---

const MEMORY_TRIGGERS = [
  /remember|preference|prefer|important|decision|decided|always|never/i,
  /[\w.-]+@[\w.-]+\.\w+/,                                       // email
  /(?:my)\s*(?:name|live|from|birthday|phone|email)/i,         // identity
  /(?:i)\s*(?:like|hate|love|want|need|think|believe)/i,       // preference
  /(?:favorite|favourite|love|hate|enjoy|dislike)/i,
];

export function shouldCapture(
  text: string, mode: "semantic" | "keyword",
): { capture: boolean; reason: string } {
  const normalized = text.trim();
  if (!normalized) return { capture: false, reason: "empty" };

  const compact = normalized.replace(/\s+/g, "");
  const isCJK = /[぀-ヿ㐀-鿿豈-﫿가-힯]/.test(compact);

  // Length bounds
  const minLen = isCJK ? 4 : 10;
  if (compact.length < minLen) return { capture: false, reason: "too_short" };
  if (normalized.length > 24000) return { capture: false, reason: "too_long" };

  // Command detection
  if (/^\/[a-z0-9_-]{1,64}\b/i.test(normalized)) {
    return { capture: false, reason: "command" };
  }

  // Non-content (punctuation/symbols only)
  if (/^[\p{P}\p{S}\s]+$/u.test(normalized)) {
    return { capture: false, reason: "non_content" };
  }

  // Question-only
  if (/^(who|what|when|where|why|how|is|are|does|did|can|could|would|should)\b.{0,200}[?？]$/i.test(normalized)) {
    return { capture: false, reason: "question_only" };
  }

  // Keyword mode gate
  if (mode === "keyword") {
    const hasTrigger = MEMORY_TRIGGERS.some(re => re.test(normalized));
    return { capture: hasTrigger, reason: hasTrigger ? "trigger_matched" : "no_trigger" };
  }

  // Semantic mode — always capture
  return { capture: true, reason: "semantic" };
}

// --- Write Queue ---

interface QueuedTurn {
  role: string;
  content: string;
}

export class WriteQueue {
  private client: OVClient;
  private sessionId: string;
  private queue: QueuedTurn[] = [];
  private flushTimer: ReturnType<typeof setInterval> | null = null;
  private flushing = false;
  private intervalMs: number;
  private threshold: number;

  constructor(
    client: OVClient, sessionId: string,
    intervalMs: number, threshold: number,
  ) {
    this.client = client;
    this.sessionId = sessionId;
    this.intervalMs = intervalMs;
    this.threshold = threshold;
  }

  start(): void {
    if (this.intervalMs > 0) {
      this.flushTimer = setInterval(() => this.flush(), this.intervalMs);
    }
  }

  enqueue(role: string, content: string): void {
    this.queue.push({ role, content });
    if (this.queue.length >= this.threshold) {
      this.flush(); // fire-and-forget
    }
  }

  async flush(): Promise<void> {
    if (this.flushing || this.queue.length === 0) return;
    this.flushing = true;

    const batch = this.queue.splice(0);
    for (let i = 0; i < batch.length; i++) {
      const turn = batch[i];
      const ok = await this.client.addMessage(
        this.sessionId, turn.role, turn.content,
      );
      if (!ok) {
        // Re-queue failed turn + remaining unprocessed turns at front for retry
        this.queue.unshift(...batch.slice(i));
        break;
      }
    }
    this.flushing = false;
  }

  cancelPending(): void {
    if (this.flushTimer) {
      clearInterval(this.flushTimer);
      this.flushTimer = null;
    }
  }
}

// --- SyncManager ---

export class SyncManager {
  private client: OVClient;
  private config: OVConfig;
  private ovSessionId: string | null = null;
  private pendingTokens = 0;
  private syncedTurnCount = 0;
  private writeQueue: WriteQueue | null = null;

  constructor(client: OVClient, config: OVConfig) {
    this.client = client;
    this.config = config;
  }

  get sessionId(): string | null { return this.ovSessionId; }

  async ensureSession(piSessionId: string): Promise<boolean> {
    if (this.ovSessionId) return true;

    const id = `pi-${piSessionId}`;
    const created = await this.client.createSession(id);
    if (!created) return false;

    this.ovSessionId = id;
    this.writeQueue = new WriteQueue(
      this.client, id,
      this.config.writeQueueFlushInterval,
      this.config.writeQueueFlushThreshold,
    );
    this.writeQueue.start();
    return true;
  }

  /**
   * Sync one turn to OV. Returns the estimated token count of what was
   * synced (0 when the turn was filtered out) so the takeover layer can do
   * commit-threshold accounting.
   *
   * Faithful mode (takeover enabled): the archive overview becomes part of
   * the model's effective history, so every turn must be captured — only
   * empty content and slash-commands are skipped, `captureMode` is ignored.
   */
  async syncTurn(
    userText: string, assistantText: string, toolLines: string[],
    turnIndex: number,
  ): Promise<number> {
    if (!this.ovSessionId || !this.writeQueue) return 0;

    // Dedup guard
    if (turnIndex <= this.syncedTurnCount) return 0;

    // Capture filter on user text
    const faithful = this.config.takeoverEnabled;
    if (faithful) {
      const t = userText.trim();
      if (!t || /^\/[a-z0-9_-]{1,64}\b/i.test(t)) {
        this.syncedTurnCount = turnIndex;
        return 0;
      }
    } else {
      const filterResult = shouldCapture(userText, this.config.captureMode);
      if (!filterResult.capture) {
        this.syncedTurnCount = turnIndex;
        return 0;
      }
    }

    // Strip injected blocks
    const cleanUser = stripInjectedBlocks(userText);
    if (!cleanUser) {
      this.syncedTurnCount = turnIndex;
      return 0;
    }

    // Enqueue user message
    this.writeQueue.enqueue("user", cleanUser);

    // Enqueue assistant message (if configured)
    if (this.config.captureAssistantTurns) {
      const cleanAssistant = stripInjectedBlocks(assistantText);
      const combined = toolLines.length > 0
        ? `${toolLines.join("\n")}\n${cleanAssistant}`
        : cleanAssistant;
      if (combined.trim()) {
        this.writeQueue.enqueue("assistant", combined);
      }
    }

    // Track tokens
    const totalText = cleanUser + assistantText + toolLines.join("");
    const turnTokens = estimateTokens(totalText);
    this.pendingTokens += turnTokens;
    this.syncedTurnCount = turnIndex;

    // Check commit threshold (takeover mode owns commits instead)
    if (!this.config.takeoverEnabled &&
        this.config.commitTokenThreshold > 0 &&
        this.pendingTokens >= this.config.commitTokenThreshold) {
      await this.writeQueue.flush();
      await this.commit(false);
    }
    return turnTokens;
  }

  /** Flush queued turns without cancelling the flush timer (mid-session commits). */
  async flushQueue(): Promise<void> {
    await this.writeQueue?.flush();
  }

  async commit(_wait: boolean = false): Promise<string | null> {
    if (!this.ovSessionId) return null;
    const result = await this.client.commitSession(this.ovSessionId);
    if (result) this.pendingTokens = 0;
    return result?.archive_uri ?? null;
  }

  async shutdown(): Promise<void> {
    if (!this.writeQueue) return;
    this.writeQueue.cancelPending();
    await this.writeQueue.flush();
  }
}
