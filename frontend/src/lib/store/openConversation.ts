/** Remembers which conversation this tab has open, so a page reload lands back
 * in it instead of an empty new chat.
 *
 * `sessionStorage`, not `localStorage`, on purpose: it is **per-tab**, so a
 * second tab still gets its own conversation instead of the two fighting over
 * one id. It also dies with the tab, which is the right lifetime — the id is a
 * convenience for surviving a reload, not a durable user preference (the
 * durable copy is Valkey's, reachable through the history sidebar).
 *
 * Every access is wrapped: Safari in private browsing, and any browser with
 * site data blocked, throw on `sessionStorage` rather than returning null.
 * Losing the id is a degraded-but-working experience (you get a new chat, like
 * before this existed), so it must never break bootstrap.
 */

const STORAGE_KEY = "prometheus.openConversationId";

export function readOpenConversationId(): string | null {
  try {
    const v = sessionStorage.getItem(STORAGE_KEY);
    return v ? v : null;
  } catch {
    return null;
  }
}

export function saveOpenConversationId(id: string | null): void {
  try {
    if (id) sessionStorage.setItem(STORAGE_KEY, id);
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Non-fatal: see the module comment.
  }
}
