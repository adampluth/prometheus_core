import { useState } from "react";
import { Cpu, FolderOpen } from "lucide-react";
import { useChatState } from "@/lib/store/chat";
import type { ArtifactMeta } from "@/lib/api/types";

type StatusTab = "jobs" | "artifacts";

function EmptyState({ icon, title, hint }: { icon: React.ReactNode; title: string; hint: string }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-4 px-6 text-center">
      <div className="flex items-center justify-center rounded-2xl border border-base-300 bg-base-200 p-4 text-base-content/40">
        {icon}
      </div>
      <div className="space-y-1.5">
        <p className="m-0 font-semibold text-base-content">{title}</p>
        <p className="m-0 text-xs text-base-content/60">{hint}</p>
      </div>
    </div>
  );
}

/** Every artifact in the open conversation's transcript, newest last, deduped
 * by id (a resumed conversation attaches its whole catalog to the last
 * assistant turn — see `itemsFromTranscript` in `lib/store/chat.ts`). */
function useConversationArtifacts(): ArtifactMeta[] {
  const { items } = useChatState();
  const byId = new Map<string, ArtifactMeta>();
  for (const item of items) {
    if (item.kind !== "assistant") continue;
    for (const artifact of item.artifacts) byId.set(artifact.artifact_id, artifact);
  }
  return [...byId.values()];
}

function ArtifactCard({ artifact }: { artifact: ArtifactMeta }) {
  return (
    <a
      href={artifact.content_url}
      target="_blank"
      rel="noreferrer"
      className="card border border-base-300 bg-base-200 transition-colors hover:border-primary/40 hover:bg-base-300"
    >
      <div className="card-body gap-1.5 p-3">
        <div className="flex items-start justify-between gap-2">
          <span className="min-w-0 truncate text-sm font-semibold text-base-content">
            {artifact.name}
          </span>
          <span className="badge badge-outline badge-sm shrink-0 border-base-300 font-mono text-[10px] text-primary">
            {artifact.render_hint}
          </span>
        </div>
        {artifact.description && (
          <p className="m-0 line-clamp-2 text-xs leading-snug text-base-content/60">
            {artifact.description}
          </p>
        )}
      </div>
    </a>
  );
}

/**
 * Right-hand status rail for Home: a DaisyUI tab bar switching between Active
 * Jobs and Artifacts. Artifacts are live off the conversation transcript; job
 * cards get wired in as that data source lands.
 */
export function StatusPanel() {
  const [tab, setTab] = useState<StatusTab>("jobs");
  const artifacts = useConversationArtifacts();

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div role="tablist" className="tabs tabs-border shrink-0 px-6 pt-6">
        <button
          role="tab"
          className={`tab text-xs font-bold uppercase tracking-wide ${tab === "jobs" ? "tab-active" : ""}`}
          onClick={() => setTab("jobs")}
        >
          Active Jobs
        </button>
        <button
          role="tab"
          className={`tab text-xs font-bold uppercase tracking-wide ${tab === "artifacts" ? "tab-active" : ""}`}
          onClick={() => setTab("artifacts")}
        >
          Artifacts{artifacts.length > 0 && ` (${artifacts.length})`}
        </button>
      </div>

      {tab === "jobs" ? (
        <EmptyState
          icon={<Cpu size={32} />}
          title="No Active Jobs"
          hint="Assigned solver pipelines will display here"
        />
      ) : artifacts.length === 0 ? (
        <EmptyState
          icon={<FolderOpen size={32} />}
          title="No Artifacts Yet"
          hint="Generated artifacts will display here"
        />
      ) : (
        <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-6 py-4">
          {artifacts.map((artifact) => (
            <ArtifactCard key={artifact.artifact_id} artifact={artifact} />
          ))}
        </div>
      )}
    </div>
  );
}
