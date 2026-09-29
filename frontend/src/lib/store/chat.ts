/**
 * Chat store — replaces Chainlit's Recoil atoms + `useChatMessages`/`useChatInteract`.
 *
 * A minimal `useSyncExternalStore`-backed store (Decision A: drop Recoil) that owns
 * conversation state and the network layer:
 *
 * - `bootstrap()` loads `/api/config` + creates a session.
 * - `connect()` opens the `/api/events` SSE stream; `disconnect()` closes it.
 * - `sendTurn` / `submitFollowup` / `decidePlan` POST `/api/chat/messages` and reduce
 *   the `ChatMessageResponse` into `ChatItem`s.
 * - SSE `status` events feed the in-flight turn's progress (ToolStep); `task_progress`
 *   / `task_done` drive backgrounded tasks.
 *
 * `ChatItem` is the app's own message type; `Chat.tsx` renders it, and the artifact
 * adapter (Step 05) converts `AssistantItem.artifacts` → viewer elements.
 */

import { useSyncExternalStore } from "react";
import { api, ApiError } from "@/lib/api/client";
import { openEventStream } from "@/lib/api/events";
import type {
  ArtifactMeta,
  ChatMessageRequest,
  ChatMessageResponse,
  ConfigResponse,
  ConversationMessage,
  FormField,
  StatusEvent,
  TaskDoneEvent,
  TaskProgressEvent,
  UploadDescriptor,
} from "@/lib/api/types";

// --- item model -------------------------------------------------------------

interface BaseItem {
  id: string;
}
export interface UserItem extends BaseItem {
  kind: "user";
  text: string;
}
export interface AssistantItem extends BaseItem {
  kind: "assistant";
  text: string;
  artifacts: ArtifactMeta[];
  recommendedMaterials: string[];
  followUpSuggestions: string[];
}
/** In-flight turn progress or a backgrounded task — renders as the ToolStep UI. */
export interface ToolItem extends BaseItem {
  kind: "tool";
  turnId?: string;
  taskId?: string;
  name: string;
  history: string[];
  done: boolean;
}
export interface NeedsInfoItem extends BaseItem {
  kind: "needs_info";
  turnId: string;
  message: string;
  fields: FormField[];
  submitted: boolean;
}
export interface PlanItem extends BaseItem {
  kind: "plan";
  turnId: string;
  message: string;
  plan: Record<string, unknown>;
  autoApprove: boolean;
  decided: null | "approved" | "rejected";
}
export interface ErrorItem extends BaseItem {
  kind: "error";
  text: string;
}
export type ChatItem =
  | UserItem
  | AssistantItem
  | ToolItem
  | NeedsInfoItem
  | PlanItem
  | ErrorItem;

// --- state ------------------------------------------------------------------

export interface ChatState {
  ready: boolean;
  items: ChatItem[];
  awaiting: boolean;
  conversationId: string | null;
  config: ConfigResponse | null;
  autoApprove: boolean;
  /** Bumped whenever the conversation list may have changed (new/renamed title,
   * a completed turn, a switch to another conversation) — components rendering
   * the history list re-fetch when this changes. */
  historyVersion: number;
}

const initialState: ChatState = {
  ready: false,
  items: [],
  awaiting: false,
  conversationId: null,
  config: null,
  autoApprove: false,
  historyVersion: 0,
};

let state: ChatState = initialState;
const listeners = new Set<() => void>();
const awaitedTaskIds = new Set<string>();
let closeStream: (() => void) | null = null;

function emit(): void {
  for (const l of listeners) l();
}
function set(patch: Partial<ChatState>): void {
  state = { ...state, ...patch };
  emit();
}
function setItems(update: (items: ChatItem[]) => ChatItem[]): void {
  set({ items: update(state.items) });
}

let idCounter = 0;
function nextId(prefix: string): string {
  idCounter += 1;
  return `${prefix}-${idCounter}`;
}

function bumpHistory(): void {
  set({ historyVersion: state.historyVersion + 1 });
}

// --- response → items reducer ----------------------------------------------

function assistantItem(text: string, artifacts: ArtifactMeta[] = []): AssistantItem {
  return {
    id: nextId("assistant"),
    kind: "assistant",
    text,
    artifacts,
    recommendedMaterials: [],
    followUpSuggestions: [],
  };
}

function assistantFrom(res: ChatMessageResponse, text: string): AssistantItem {
  return {
    ...assistantItem(text, res.artifacts ?? []),
    recommendedMaterials: res.recommended_materials ?? [],
    followUpSuggestions: res.follow_up_suggestions ?? [],
  };
}

/** Map a resumed conversation's normalized transcript to renderable items; the
 * backend has already attached each artifact to the reply that produced it. */
function itemsFromTranscript(messages: ConversationMessage[]): ChatItem[] {
  return messages.map((m) =>
    m.role === "user"
      ? { id: nextId("user"), kind: "user", text: m.content }
      : assistantItem(m.content, m.artifacts ?? []),
  );
}

/** Mark the in-flight turn's ToolStep done (leaves its progress trace collapsed). */
function finalizeTurnProgress(turnId: string): void {
  setItems((items) =>
    items.map((it) =>
      it.kind === "tool" && it.turnId === turnId ? { ...it, done: true } : it,
    ),
  );
}

/** Append the item(s) a terminal ChatMessageResponse maps to. */
function applyResponse(res: ChatMessageResponse): void {
  finalizeTurnProgress(res.turn_id);
  // A completed turn may have just gotten this conversation its first LLM
  // title, or changed its inferred status — let the history list re-fetch.
  bumpHistory();

  switch (res.status) {
    case "answer":
    case "recommendation": {
      const text = res.response_text ?? res.message ?? "";
      setItems((items) => [...items, assistantFrom(res, text)]);
      break;
    }
    case "completed": {
      const text = res.report ?? res.message ?? "";
      setItems((items) => [...items, assistantFrom(res, text)]);
      break;
    }
    case "needs_info": {
      const fields = res.followup_questions ?? [];
      if (fields.length === 0) {
        // Backend still sends needs_info with zero questions; render the message as a
        // normal bubble rather than an empty form that would hang (frontend note).
        setItems((items) => [...items, assistantFrom(res, res.message ?? "")]);
      } else {
        setItems((items) => [
          ...items,
          {
            id: nextId("needs_info"),
            kind: "needs_info",
            turnId: res.turn_id,
            message: res.message ?? "",
            fields,
            submitted: false,
          },
        ]);
      }
      break;
    }
    case "plan_ready": {
      const item: PlanItem = {
        id: nextId("plan"),
        kind: "plan",
        turnId: res.turn_id,
        message: res.message ?? "",
        plan: res.plan ?? {},
        autoApprove: Boolean(res.auto_approve),
        decided: null,
      };
      setItems((items) => [...items, item]);
      // auto_approve: execute immediately without prompting (no buttons rendered).
      if (item.autoApprove) {
        void decidePlan(item.id, true);
      }
      break;
    }
    case "executing": {
      setItems((items) => [
        ...items,
        {
          id: nextId("tool"),
          kind: "tool",
          turnId: res.turn_id,
          name: res.message ?? "Executing the plan…",
          history: [],
          done: false,
        },
      ]);
      break;
    }
    case "backgrounded": {
      if (res.task_id) awaitedTaskIds.add(res.task_id);
      setItems((items) => [
        ...items,
        {
          id: nextId("tool"),
          kind: "tool",
          taskId: res.task_id ?? undefined,
          name: res.message ?? "Running in the background…",
          history: [],
          done: false,
        },
      ]);
      break;
    }
    case "error": {
      setItems((items) => [
        ...items,
        { id: nextId("error"), kind: "error", text: res.message ?? "An error occurred." },
      ]);
      break;
    }
  }
}

function appendError(text: string): void {
  setItems((items) => [...items, { id: nextId("error"), kind: "error", text }]);
}

// --- SSE handlers -----------------------------------------------------------

function onStatus(e: StatusEvent): void {
  const history = e.history ?? [e.message];
  setItems((items) => {
    const idx = items.findIndex((it) => it.kind === "tool" && it.turnId === e.turn_id);
    if (idx >= 0) {
      const next = [...items];
      next[idx] = { ...(next[idx] as ToolItem), name: e.message, history };
      return next;
    }
    return [
      ...items,
      {
        id: nextId("tool"),
        kind: "tool",
        turnId: e.turn_id,
        name: e.message,
        history,
        done: false,
      },
    ];
  });
}

function onTaskProgress(e: TaskProgressEvent): void {
  if (!awaitedTaskIds.has(e.task_id)) return;
  setItems((items) =>
    items.map((it) =>
      it.kind === "tool" && it.taskId === e.task_id
        ? { ...it, name: e.message, history: [...it.history, e.message] }
        : it,
    ),
  );
}

function onTaskDone(e: TaskDoneEvent): void {
  if (!awaitedTaskIds.has(e.task_id)) return;
  awaitedTaskIds.delete(e.task_id);
  // Close out the task's progress ToolStep.
  setItems((items) =>
    items.map((it) =>
      it.kind === "tool" && it.taskId === e.task_id ? { ...it, done: true } : it,
    ),
  );
  if (e.response) {
    applyResponse(e.response);
  } else if (e.message) {
    appendError(e.message);
  }
}

// --- actions ----------------------------------------------------------------

async function bootstrap(profile?: string): Promise<void> {
  try {
    const config = await api.getConfig();
    const session = await api.createSession(profile);
    set({
      ready: true,
      config,
      conversationId: session.conversation_id,
      autoApprove: session.auto_approve_steps,
    });
  } catch (err) {
    // Surface a bootstrap failure but still let the UI render (empty).
    set({ ready: true });
    appendError(err instanceof Error ? err.message : "Failed to start session.");
  }
}

function connect(): void {
  if (closeStream) return;
  closeStream = openEventStream({ onStatus, onTaskProgress, onTaskDone });
}

function disconnect(): void {
  closeStream?.();
  closeStream = null;
}

async function post(body: ChatMessageRequest): Promise<void> {
  // Captured before the await: if the user starts a new chat or resumes a
  // different past conversation (chatStore.reset/loadConversation) while this
  // turn is still in flight, `state.conversationId` changes out from under us.
  // The response, when it lands, belongs to `conversationId` — not to whatever
  // is now on screen — so every effect below is guarded against that. The
  // backend applies the same guard on its side (see `services/chat.py`'s
  // `stale` handling) for its own session-state mutations; this is the
  // matching frontend half, for what gets rendered.
  const conversationId = state.conversationId;
  const isCurrent = () => state.conversationId === conversationId;

  set({ awaiting: true });
  try {
    const res = await api.sendMessage(body);
    if (isCurrent()) {
      applyResponse(res);
    } else {
      // Not lost — it's safely persisted server-side and will show up if this
      // conversation is reopened (see loadConversation) — just not rendered
      // into whatever conversation is currently on screen. Still worth letting
      // the history list know something changed (e.g. its status).
      bumpHistory();
    }
  } catch (err) {
    if (isCurrent()) {
      const text =
        err instanceof ApiError
          ? `Error communicating with agent: ${err.message}`
          : "Error communicating with agent.";
      appendError(text);
    }
  } finally {
    if (isCurrent()) {
      set({ awaiting: false });
    }
  }
}

/** Send a user turn (optionally with staged uploads). */
async function sendTurn(query: string, uploads: UploadDescriptor[] = []): Promise<void> {
  const text = query.trim();
  if (!text && uploads.length === 0) return;
  setItems((items) => [
    ...items,
    { id: nextId("user"), kind: "user", text: text || "(uploaded file)" },
  ]);
  await post({ query: text, upload_ids: uploads.length ? uploads : null });
}

/** Answer a needs_info form (keys are FormField.id). */
async function submitFollowup(itemId: string, answers: Record<string, unknown>): Promise<void> {
  let query = "";
  setItems((items) =>
    items.map((it) => {
      if (it.id === itemId && it.kind === "needs_info") {
        query = it.message;
        return { ...it, submitted: true };
      }
      return it;
    }),
  );
  await post({ query, followup_answers: answers });
}

/** Approve or reject a pending plan. */
async function decidePlan(itemId: string, approved: boolean): Promise<void> {
  setItems((items) =>
    items.map((it) =>
      it.id === itemId && it.kind === "plan"
        ? { ...it, decided: approved ? "approved" : "rejected" }
        : it,
    ),
  );
  await post({ query: "", plan_approved: approved, plan_rejected: !approved });
}

/** Client-side stop — no server cancel endpoint exists yet (Decision D). */
function stop(): void {
  set({ awaiting: false });
}

/** New chat: fresh conversation_id + cleared transcript (no full reload). */
async function reset(profile?: string): Promise<void> {
  disconnect();
  awaitedTaskIds.clear();
  set({ items: [], awaiting: false });
  try {
    const session = await api.createSession(profile);
    set({ conversationId: session.conversation_id, autoApprove: session.auto_approve_steps });
  } catch (err) {
    appendError(err instanceof Error ? err.message : "Failed to start a new conversation.");
  }
  connect();
  bumpHistory();
}

// Bumped per loadConversation call, so a slow response for a conversation the user
// has already clicked away from can't overwrite the one they're now looking at.
let loadSeq = 0;

/** Reopen a past conversation: rebinds the session server-side (so the next
 * turn continues it) and replays its transcript. Mirrors `reset()`'s shape. */
async function loadConversation(conversationId: string): Promise<void> {
  const seq = ++loadSeq;
  disconnect();
  awaitedTaskIds.clear();
  set({ items: [], awaiting: false });
  try {
    const detail = await api.resumeConversation(conversationId);
    if (seq !== loadSeq) return; // superseded by a later click; that load owns the view
    set({
      conversationId: detail.conversation_id,
      autoApprove: detail.auto_approve_steps,
      items: itemsFromTranscript(detail.messages),
    });
  } catch (err) {
    if (seq !== loadSeq) return;
    appendError(err instanceof Error ? err.message : "Failed to open conversation.");
  }
  connect();
  bumpHistory();
}

export const chatStore = {
  bootstrap,
  connect,
  disconnect,
  sendTurn,
  loadConversation,
  submitFollowup,
  decidePlan,
  stop,
  reset,
  /** Test/escape hatch: reset in-memory state (not used by the app). */
  _clear(): void {
    disconnect();
    awaitedTaskIds.clear();
    state = initialState;
    emit();
  },
};

// --- React binding ----------------------------------------------------------

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}
function getSnapshot(): ChatState {
  return state;
}

/** Subscribe a component to the whole chat state. */
export function useChatState(): ChatState {
  return useSyncExternalStore(subscribe, getSnapshot);
}
