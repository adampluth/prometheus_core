/**
 * src/viewers/mesh/MeshViewerElement.tsx
 *
 * Renders the `MeshViewer` Chainlit custom element via vtk.js — source-agnostic
 * across Exodus and STL, since both emit the same manifest.json + meta.json
 * wire contract (see components/mesh_view.py / exodus_integration.py /
 * stl_integration.py). With multiple timesteps it shows playback controls;
 * with field variables it shows a variable selector and colorbar; with
 * neither (an STL) it just renders geometry.
 *
 * Loading is lazy: an artifact element carries only `meshUrl` (the artifact's
 * `mesh_url`). Nothing is fetched until the viewer comes within ~200px of the
 * viewport; the first fetch of a mesh makes the backend convert it (which can take
 * a while for a large Exodus file), later ones hit its conversion cache. Fetched
 * props are kept per URL for the page's lifetime, so scrolling back or reopening
 * the conversation doesn't ask again. Elements that arrive with an inline
 * `manifest`/`baseUrl` render straight away.
 *
 * Known pipeline limitations (not fixable from the frontend):
 *   - `representation: "surface" | "volume"` is accepted by the backend's
 *     ConvertRequest but never read — everything is surface geometry.
 *   - Per-block visibility toggling isn't possible; the VTP output has no
 *     per-cell block ID, and block selection only happens at conversion time.
 *
 * TODO: useTheme() below does its own dark-mode MutationObserver instead of
 * using the app's real theme source — same TODO as the IFC viewer had before
 * it was ported; swap once confirmed (Recoil atom / DaisyUI data-theme).
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { Maximize2, Minimize2, RotateCcw } from "lucide-react";
import "./mesh.css";
import MeshViewer, { MeshViewerHandles } from "./MeshViewer";
import MeshViewerControls, { VariableOption } from "./MeshViewerControls";
import type { ColorPreset, PickResult } from "./MeshViewerEngine";
import type { ViewerProps } from "@/lib/chainlit/elements";

type MeshProps = {
  meshUrl?: string;
  downloadUrl?: string;
  datasetId?: string;
  baseUrl?: string;
  metadata?: {
    num_timesteps?: number;
    time_values?: number[];
  };
  manifest?: {
    files?: { index: number; timestep_index: number; time_value: number; filename: string }[];
    variables?: { point?: string[]; cell?: string[] };
    array_ranges?: Record<string, [number, number]>;
  };
  height?: string;
  title?: string;
  showControls?: boolean;
  allowPicking?: boolean;
  initialTimestep?: number;
  initialVariable?: string | null;
  initialColorPreset?: ColorPreset;
};

function useTheme() {
  const [isDark, setIsDark] = useState(() => {
    if (typeof document === "undefined") return true;
    const html = document.documentElement;
    if (html.classList.contains("dark")) return true;
    if (html.getAttribute("data-theme") === "dark") return true;
    return !!window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  });

  useEffect(() => {
    const observer = new MutationObserver(() => {
      const html = document.documentElement;
      setIsDark(html.classList.contains("dark") || html.getAttribute("data-theme") === "dark");
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class", "data-theme"] });
    return () => observer.disconnect();
  }, []);

  return isDark;
}

/** Viewer props fetched from a `mesh_url`, one request per URL per page load. */
const meshPropsCache = new Map<string, Promise<MeshProps>>();

async function fetchMeshProps(meshUrl: string): Promise<MeshProps> {
  const res = await fetch(meshUrl, { credentials: "include" });
  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body — keep statusText */
    }
    throw new Error(detail);
  }
  return (await res.json()) as MeshProps;
}

function loadMeshProps(meshUrl: string): Promise<MeshProps> {
  let pending = meshPropsCache.get(meshUrl);
  if (!pending) {
    pending = fetchMeshProps(meshUrl);
    // Forget failures so a retry (or a later remount) asks again.
    pending.catch(() => meshPropsCache.delete(meshUrl));
    meshPropsCache.set(meshUrl, pending);
  }
  return pending;
}

type LoadState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; props: MeshProps }
  | { status: "error"; message: string };

export function MeshViewerElement({ element }: ViewerProps) {
  const props = ((element as { props?: MeshProps }).props) ?? {};
  if (props.manifest || props.baseUrl) return <MeshViewerBody props={props} />;
  return <LazyMeshViewer props={props} />;
}

/** Placeholder until scrolled near the viewport, then fetches `meshUrl` and mounts the viewer. */
function LazyMeshViewer({ props }: { props: MeshProps }) {
  const { meshUrl, downloadUrl } = props;
  const title = props.title ?? "Mesh Viewer";
  const height = props.height ?? "450px";
  const placeholderRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [state, setState] = useState<LoadState>({ status: "idle" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const node = placeholderRef.current;
    if (visible || !node) return;
    if (typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "200px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [visible]);

  useEffect(() => {
    if (!visible) return;
    if (!meshUrl) {
      setState({ status: "error", message: "No mesh is available for this artifact." });
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    loadMeshProps(meshUrl).then(
      (loaded) => !cancelled && setState({ status: "ready", props: loaded }),
      (err: unknown) =>
        !cancelled && setState({ status: "error", message: err instanceof Error ? err.message : String(err) }),
    );
    return () => {
      cancelled = true;
    };
  }, [visible, meshUrl, attempt]);

  if (state.status === "ready") {
    // The fetched props carry the dataset; the element's own display options win.
    return <MeshViewerBody props={{ ...state.props, ...props, title }} />;
  }

  return (
    <div
      ref={placeholderRef}
      className="mesh-viewer-root relative my-2 w-full max-w-full rounded-lg overflow-hidden border border-base-300 bg-base-200/40 flex flex-col"
    >
      <div className="flex items-center px-4 py-2 border-b border-base-300 bg-base-200/40 text-sm font-medium text-base-content">
        {title}
      </div>
      <div
        className="flex flex-col items-center justify-center gap-3 w-full bg-base-300/40 px-4 text-sm text-base-content/70"
        style={{ height }}
      >
        {state.status === "error" ? (
          <>
            <div className="text-error/80 bg-error/10 border border-error/30 rounded px-3 py-2 max-w-full break-words">
              {state.message}
            </div>
            <div className="flex items-center gap-2">
              <button type="button" className="btn btn-xs" onClick={() => setAttempt((n) => n + 1)}>
                Retry
              </button>
              {downloadUrl && (
                <a className="btn btn-xs btn-ghost" href={downloadUrl} download>
                  Download file
                </a>
              )}
            </div>
          </>
        ) : state.status === "loading" ? (
          <>
            <span className="loading loading-spinner loading-md" />
            <span>Preparing mesh… (the first view of a large mesh can take a while)</span>
          </>
        ) : (
          <span>Mesh loads when scrolled into view</span>
        )}
      </div>
    </div>
  );
}

function MeshViewerBody({ props }: { props: MeshProps }) {
  const datasetId = props.datasetId ?? "mesh";
  const baseUrl = props.baseUrl ?? "";
  const metadata = props.metadata ?? {};
  const manifest = props.manifest ?? {};
  const height = props.height ?? "450px";
  const title = props.title ?? "Mesh Viewer";
  const showControls = props.showControls ?? true;
  const allowPicking = props.allowPicking ?? true;

  const isDark = useTheme();
  const meshRef = useRef<MeshViewerHandles>(null);
  const viewerContainerRef = useRef<HTMLDivElement>(null);

  const numTimesteps = metadata.num_timesteps || 1;
  const timeValues = metadata.time_values || [];
  const files = manifest.files || [];
  const pointVariables = manifest.variables?.point || [];
  const cellVariables = manifest.variables?.cell || [];
  const arrayRanges = manifest.array_ranges || {};

  const [currentTimestep, setCurrentTimestep] = useState(props.initialTimestep ?? 0);
  const [currentVariable, setCurrentVariable] = useState<string | null>(props.initialVariable ?? null);
  const [colorPreset, setColorPreset] = useState<ColorPreset>(props.initialColorPreset ?? "coolToWarm");
  const [scalarRange, setScalarRange] = useState<[number, number]>([0, 1]);
  const [isPlaying, setIsPlaying] = useState(false);
  const [pickedInfo, setPickedInfo] = useState<PickResult | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Theme → engine background/edge color
  useEffect(() => {
    meshRef.current?.setBackground(isDark ? [0.08, 0.08, 0.12] : [0.91, 0.91, 0.94]);
    meshRef.current?.setEdgeColor(isDark ? [0.72, 0.76, 0.84] : [0.08, 0.1, 0.14]);
  }, [isDark]);

  // Fullscreen
  const toggleFullscreen = useCallback(() => {
    if (!viewerContainerRef.current) return;
    if (!document.fullscreenElement) {
      viewerContainerRef.current.requestFullscreen().catch((err) => console.error("Fullscreen failed:", err));
    } else {
      document.exitFullscreen();
    }
  }, []);

  useEffect(() => {
    const handler = () => setIsFullscreen(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", handler);
    return () => document.removeEventListener("fullscreenchange", handler);
  }, []);

  // Load the current timestep whenever it (or the dataset) changes.
  useEffect(() => {
    const fileInfo = files.find((f) => f.timestep_index === currentTimestep) || files[currentTimestep];
    if (!fileInfo || !baseUrl) return;

    setError(null);
    meshRef.current
      ?.loadTimestep(`${baseUrl}/${fileInfo.filename}`)
      .then(() => {
        if (currentVariable) {
          const range = arrayRanges[currentVariable];
          const applied = meshRef.current?.applyVariableColoring(currentVariable, range);
          if (applied) setScalarRange(applied);
        }
      })
      .catch((err) => {
        console.error(`Failed to load timestep ${currentTimestep}:`, err);
        setError(`Failed to load timestep: ${err.message}`);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentTimestep, baseUrl, datasetId]);

  // Re-apply coloring when the variable selection changes.
  useEffect(() => {
    if (!currentVariable) {
      meshRef.current?.clearVariableColoring();
      return;
    }
    const range = arrayRanges[currentVariable];
    const applied = meshRef.current?.applyVariableColoring(currentVariable, range);
    if (applied) setScalarRange(applied);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentVariable]);

  useEffect(() => {
    meshRef.current?.setColorPreset(colorPreset);
  }, [colorPreset]);

  useEffect(() => {
    meshRef.current?.setScalarRange(scalarRange);
  }, [scalarRange]);

  // Playback
  useEffect(() => {
    if (!isPlaying || numTimesteps <= 1) return;
    const interval = setInterval(() => {
      setCurrentTimestep((prev) => (prev + 1) % numTimesteps);
    }, 200);
    return () => clearInterval(interval);
  }, [isPlaying, numTimesteps]);

  const variableOptions: VariableOption[] = [
    ...pointVariables.map((v) => ({ value: v, label: v, group: "point" as const })),
    ...cellVariables.map((v) => ({ value: v, label: v, group: "cell" as const })),
  ];

  const formatTime = (t: number | undefined) => {
    if (t === undefined) return undefined;
    return t < 0.001 || t > 1000 ? t.toExponential(2) : t.toFixed(3);
  };

  return (
    <div
      ref={viewerContainerRef}
      className="mesh-viewer-root relative my-2 w-full max-w-full rounded-lg overflow-hidden border border-base-300 bg-base-200/40 flex flex-col"
      style={isFullscreen ? undefined : undefined}
    >
      <div className="flex items-center justify-between px-4 py-2 border-b border-base-300 bg-base-200/40">
        <div className="flex items-center gap-2 text-sm font-medium text-base-content">
          <span>{title}</span>
          <span className="badge badge-sm badge-primary badge-outline">{datasetId}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            className="btn btn-ghost btn-xs btn-square bg-base-300"
            onClick={() => meshRef.current?.resetCamera()}
            title="Reset camera"
            aria-label="Reset camera"
          >
            <RotateCcw size={14} />
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-xs btn-square bg-base-300"
            onClick={toggleFullscreen}
            title={isFullscreen ? "Exit fullscreen" : "Fullscreen"}
            aria-label={isFullscreen ? "Exit fullscreen" : "Fullscreen"}
          >
            {isFullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </button>
        </div>
      </div>

      {error ? (
        <div className="p-4">
          <div className="text-sm text-error/80 bg-error/10 border border-error/30 rounded px-3 py-2">
            {error}
          </div>
        </div>
      ) : (
        <div className="relative w-full bg-base-300/40" style={{ height: isFullscreen ? undefined : height, flex: isFullscreen ? 1 : undefined }}>
          <MeshViewer
            ref={meshRef}
            allowPicking={allowPicking}
            onPick={setPickedInfo}
          />

          {showControls && (
            <MeshViewerControls
              numTimesteps={numTimesteps}
              currentTimestep={currentTimestep}
              onTimestepChange={(i) => { setIsPlaying(false); setCurrentTimestep(i); }}
              isPlaying={isPlaying}
              onTogglePlay={() => setIsPlaying((p) => !p)}
              timeLabel={formatTime(timeValues[currentTimestep])}
              variableOptions={variableOptions}
              currentVariable={currentVariable}
              onVariableChange={setCurrentVariable}
              colorPreset={colorPreset}
              onColorPresetChange={setColorPreset}
              scalarRange={scalarRange}
              onScalarRangeChange={setScalarRange}
            />
          )}

          {pickedInfo && (
            <div className="absolute bottom-3 left-3 flex items-center gap-3 bg-base-100/90 backdrop-blur border border-base-300 rounded-lg px-3 py-2 text-xs">
              <span><span className="text-base-content/50 mr-1">Cell</span><span className="font-mono text-primary">{pickedInfo.cellId}</span></span>
              <span>
                <span className="text-base-content/50 mr-1">Pos</span>
                <span className="font-mono text-primary">({pickedInfo.position.map((p) => p.toFixed(2)).join(", ")})</span>
              </span>
              {pickedInfo.value !== null && (
                <span><span className="text-base-content/50 mr-1">Val</span><span className="font-mono text-primary">{pickedInfo.value.toExponential(3)}</span></span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default MeshViewerElement;
