/**
 * Chat-history list rendered in `LeftPanel`'s sidebar.
 *
 * Fetches `GET /api/conversations/recent` (already user-scoped server-side —
 * see `backend/app/routers/conversations.py`) and re-fetches whenever
 * `historyVersion` changes (a turn completed, a new chat started, or a past
 * one was opened — see `lib/store/chat.ts`). Degrades to the header alone
 * when Redis is unavailable, matching the endpoint's graceful-degrade shape.
 *
 * `historyVersion` only bumps for events on the *currently open* conversation,
 * so a different conversation's own background turn finishing wouldn't
 * otherwise clear its "pending" badge (`ConversationSummary.pending`, from
 * `agent:status:{id}`) until something else happened to trigger a refetch.
 * While any row is pending, a short poll picks that up without needing full
 * per-conversation live tracking (see the "Prometheus Chat History Pending
 * Response Handling" note for why that's future work, not this).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Plus } from "lucide-react";
import { api } from "@/lib/api/client";
import { chatStore, useChatState } from "@/lib/store/chat";
import type { ConversationSummary } from "@/lib/api/types";
import { ChatHistoryItem } from "./ChatHistoryItem";

const PENDING_POLL_MS = 8000;

interface Props {
  onNewChat: () => void;
}

export function ChatHistory({ onNewChat }: Props) {
  const { conversationId, resumingConversationId, historyVersion } = useChatState();
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [loaded, setLoaded] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = useCallback(() => {
    void api
      .getRecentConversations()
      .then((res) => setConversations(res.available ? res.conversations : []))
      .catch(() => setConversations([]))
      .finally(() => setLoaded(true));
  }, []);

  // `historyVersion` is a dependency purely to trigger a refetch on every
  // history-affecting event; its value is never read here.
  useEffect(() => {
    refresh();
  }, [historyVersion, refresh]);

  const anyPending = conversations.some((c) => c.pending);
  useEffect(() => {
    if (!anyPending) return;
    pollRef.current = setInterval(refresh, PENDING_POLL_MS);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [anyPending, refresh]);

  return (
    <div className="flex flex-col gap-2 pb-3">
      <p className="m-0 px-1 text-[11px] font-semibold text-base-content/60">Chat History</p>
      <button
        onClick={onNewChat}
        className="flex items-center gap-1.5 px-1 text-xs text-base-content/60 transition-colors hover:text-base-content cursor-pointer"
      >
        <Plus size={12} />
        New Chat
      </button>

      {loaded && conversations.length > 0 && (
        <div className="flex flex-col gap-0.5">
          {conversations.map((c) => (
            <ChatHistoryItem
              key={c.conversation_id}
              conversation={c}
              // `conversationId` only flips once the resume resolves, so during the
              // fetch prefer the id being loaded — otherwise the row the user just
              // clicked sits unhighlighted for the whole round trip.
              active={c.conversation_id === (resumingConversationId ?? conversationId)}
              onSelect={() => void chatStore.loadConversation(c.conversation_id)}
              onRenamed={(title) =>
                setConversations((prev) =>
                  prev.map((it) => (it.conversation_id === c.conversation_id ? { ...it, title } : it)),
                )
              }
              onDeleted={() =>
                setConversations((prev) =>
                  prev.filter((it) => it.conversation_id !== c.conversation_id),
                )
              }
            />
          ))}
        </div>
      )}
    </div>
  );
}
