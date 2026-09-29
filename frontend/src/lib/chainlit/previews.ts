/**
 * Viewer props fetched from an artifact's `preview_url` (backend
 * `GET /api/conversations/{cid}/artifacts/{aid}/preview`).
 *
 * The first request for a source makes the backend build its preview (a large mesh
 * can take a while); after that the backend serves it from disk. Here each URL is
 * fetched at most once per page load, so scrolling a viewer back into view or
 * reopening the conversation doesn't ask again. Failures are forgotten so a retry
 * (or a later remount) asks again.
 */

export type PreviewProps = Record<string, unknown>;

const pending = new Map<string, Promise<PreviewProps>>();
const resolved = new Map<string, PreviewProps>();

async function fetchPreview(url: string): Promise<PreviewProps> {
  const res = await fetch(url, { credentials: "include" });
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
  return (await res.json()) as PreviewProps;
}

export function loadPreview(url: string): Promise<PreviewProps> {
  let request = pending.get(url);
  if (!request) {
    request = fetchPreview(url);
    request.then(
      (props) => resolved.set(url, props),
      () => pending.delete(url),
    );
    pending.set(url, request);
  }
  return request;
}

/** The props for `url` if they have already arrived (lets a remount skip a loading frame). */
export function peekPreview(url: string): PreviewProps | undefined {
  return resolved.get(url);
}
