/**
 * src/components/DeferredViewer.tsx
 *
 * Mounts a viewer only while it is near the viewport, fetching its preview first
 * when the element carries a `previewUrl` (set by the artifact adapter from the
 * artifact's `preview_url`). Used by `ElementRenderer` for `DEFERRED_KINDS` and for
 * any element with a preview; the viewers themselves know nothing about it.
 *
 * - Mounts within MOUNT_MARGIN_PX of the scroll container's visible area and
 *   unmounts beyond UNMOUNT_MARGIN_PX, so a long conversation holds only the few
 *   WebGL contexts (browsers cap them at ~16) and GPU buffers actually on screen.
 *   The gap between the two margins keeps a viewer at the edge from thrashing.
 * - While unmounted, the wrapper keeps the viewer's last measured height, so the
 *   transcript doesn't jump as viewers come and go.
 * - The fetched preview props sit *under* the element's own props (the element's
 *   title / display options win).
 */

import { Suspense, useEffect, useRef, useState, type RefObject } from "react";
import type { AnyElement, ViewerComponent } from "../lib/chainlit/elements";
import { loadPreview, peekPreview, type PreviewProps } from "../lib/chainlit/previews";

const MOUNT_MARGIN_PX = 200;
const UNMOUNT_MARGIN_PX = 1500;

/** The nearest scrolling ancestor. The chat scrolls inside a container, not the page,
 * and an observer rooted at the viewport would clip at that container — its margins
 * would never see past the container's edge. */
function scrollParent(node: HTMLElement): Element | null {
  for (let el = node.parentElement; el; el = el.parentElement) {
    const { overflowY } = getComputedStyle(el);
    if (overflowY === "auto" || overflowY === "scroll") return el;
  }
  return null;
}

function useNearViewport(ref: RefObject<HTMLElement | null>): boolean {
  const [near, setNear] = useState(false);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    if (typeof IntersectionObserver === "undefined") {
      setNear(true);
      return;
    }
    const root = scrollParent(node);
    const watch = (margin: number, onChange: (intersecting: boolean) => void) => {
      const observer = new IntersectionObserver(
        (entries) => onChange(entries[entries.length - 1].isIntersecting),
        { root, rootMargin: `${margin}px 0px` },
      );
      observer.observe(node);
      return observer;
    };
    const mount = watch(MOUNT_MARGIN_PX, (hit) => hit && setNear(true));
    const unmount = watch(UNMOUNT_MARGIN_PX, (hit) => !hit && setNear(false));
    return () => {
      mount.disconnect();
      unmount.disconnect();
    };
  }, [ref]);

  return near;
}

type PreviewState =
  | { status: "loading" }
  | { status: "ready"; props: PreviewProps }
  | { status: "error"; message: string };

/** Fetch `url`'s preview props (skipped while `url` is undefined). */
function usePreview(url: string | undefined): { state: PreviewState | null; retry: () => void } {
  const [state, setState] = useState<PreviewState | null>(null);
  const [attempt, setAttempt] = useState(0);
  const cached = url ? peekPreview(url) : undefined;

  useEffect(() => {
    if (!url || peekPreview(url)) return;
    let cancelled = false;
    setState({ status: "loading" });
    loadPreview(url).then(
      (props) => !cancelled && setState({ status: "ready", props }),
      (err: unknown) =>
        !cancelled && setState({ status: "error", message: err instanceof Error ? err.message : String(err) }),
    );
    return () => {
      cancelled = true;
    };
  }, [url, attempt]);

  if (!url) return { state: null, retry: () => {} };
  return {
    state: cached ? { status: "ready", props: cached } : (state ?? { status: "loading" }),
    retry: () => setAttempt((n) => n + 1),
  };
}

function stringProp(element: AnyElement, name: string): string | undefined {
  const value = element.props?.[name];
  return typeof value === "string" && value ? value : undefined;
}

function Placeholder({
  element,
  children,
}: {
  element: AnyElement;
  children: React.ReactNode;
}) {
  const title = stringProp(element, "title") ?? stringProp(element, "filename") ?? element.name ?? "Viewer";
  return (
    <div className="relative my-2 w-full h-full max-w-full rounded-lg overflow-hidden border border-base-300 bg-base-200/40 flex flex-col">
      <div className="flex items-center px-4 py-2 border-b border-base-300 bg-base-200/40 text-sm font-medium text-base-content">
        {title}
      </div>
      <div
        className="flex flex-1 flex-col items-center justify-center gap-3 w-full bg-base-300/40 px-4 text-sm text-base-content/70"
        style={{ minHeight: stringProp(element, "height") ?? "450px" }}
      >
        {children}
      </div>
    </div>
  );
}

export function DeferredViewer({
  element,
  Viewer,
  onSend,
}: {
  element: AnyElement;
  Viewer: ViewerComponent;
  onSend?: (text: string) => void;
}) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const near = useNearViewport(wrapperRef);
  const [height, setHeight] = useState<number | null>(null);
  const previewUrl = stringProp(element, "previewUrl");
  const { state: preview, retry } = usePreview(near ? previewUrl : undefined);

  // Remember the mounted viewer's height to reserve it once unmounted.
  useEffect(() => {
    const node = wrapperRef.current;
    if (!near || !node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => setHeight(node.getBoundingClientRect().height));
    observer.observe(node);
    return () => observer.disconnect();
  }, [near]);

  let body: React.ReactNode;
  if (!near) {
    body = (
      <Placeholder element={element}>
        <span>Loads when scrolled into view</span>
      </Placeholder>
    );
  } else if (preview?.status === "error") {
    const downloadUrl = stringProp(element, "downloadUrl") ?? element.url;
    body = (
      <Placeholder element={element}>
        <div className="text-error/80 bg-error/10 border border-error/30 rounded px-3 py-2 max-w-full break-words">
          {preview.message}
        </div>
        <div className="flex items-center gap-2">
          <button type="button" className="btn btn-xs" onClick={retry}>
            Retry
          </button>
          {downloadUrl && (
            <a className="btn btn-xs btn-ghost" href={downloadUrl} download>
              Download file
            </a>
          )}
        </div>
      </Placeholder>
    );
  } else if (preview?.status === "loading") {
    body = (
      <Placeholder element={element}>
        <span className="loading loading-spinner loading-md" />
        <span>Preparing preview… (the first view of a large file can take a while)</span>
      </Placeholder>
    );
  } else {
    const merged = preview ? { ...element, props: { ...preview.props, ...element.props } } : element;
    body = (
      <Suspense fallback={null}>
        <Viewer element={merged} onSend={onSend} />
      </Suspense>
    );
  }

  return (
    <div ref={wrapperRef} style={!near && height ? { height } : undefined}>
      {body}
    </div>
  );
}
