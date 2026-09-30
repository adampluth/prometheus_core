/**
 * Artifact adapter (frontend cutover Step 05).
 *
 * Maps a wire `ArtifactMeta` (backend contract) onto the app's `AnyElement`
 * shape so the existing `ElementRenderer` / `VIEWER_REGISTRY` route it to a viewer
 * with **no changes to the viewers themselves**. This is the seam that replaces the
 * old Chainlit `render_artifacts()` element construction.
 *
 * The backend has already done MIME/json_type/tag/extension dispatch and handed us a
 * `render_hint`; this function turns that hint into an element the classifier
 * (`classifyElement`) will recognize:
 *
 *   - file bytes (image/pdf/ifc/file/unknown) → `element.url` is the artifact's
 *     conversation-scoped `content_url`; viewers fetch from there.
 *   - inline structured data (json/thermal) → props built from `json_body`.
 *   - report/reference → dedicated lightweight viewers (markdown / citation card).
 *
 * Notes:
 *   - **previews**: when the backend has a previewer for an artifact (today: meshes,
 *     Exodus/STL → VTP) it sends a `preview_url`. The element then carries
 *     `previewUrl` + `downloadUrl`, and `ElementRenderer` fetches the preview's props
 *     once the viewer scrolls into view and merges them into the element — so no
 *     viewer, and nothing here, is special-cased for it.
 *   - **mermaid**: no agent currently emits mermaid artifacts, and the diagram source
 *     is not a wire field. We extract it best-effort from `json_body`; an empty source
 *     surfaces the viewer's own "no diagram" state.
 */

import type { ArtifactMeta } from "@/lib/api/types";
import type { AnyElement } from "./elements";

/** Best-effort mermaid diagram source from an inline body (string or `{diagram|chart|content}`). */
function mermaidSource(body: unknown): string {
  if (typeof body === "string") return body;
  if (body && typeof body === "object") {
    const o = body as Record<string, unknown>;
    for (const key of ["diagram", "chart", "content"]) {
      if (typeof o[key] === "string") return o[key] as string;
    }
  }
  return "";
}

/** Convert one wire artifact into a viewer-ready element. */
export function artifactToElement(artifact: ArtifactMeta): AnyElement {
  const element = buildElement(artifact);
  // Stamp the unique artifact id into props so `dedupeElements` keys on it:
  // `resourceSignature` derives a custom element's identity from `props.artifact_id`
  // (among others), and several viewers carry no other distinguishing prop — without
  // this, two IFC/mermaid/report artifacts sharing a component name would collapse to one.
  element.props = {
    ...(element.props ?? {}),
    artifact_id: artifact.artifact_id,
    ...(artifact.preview_url ? { previewUrl: artifact.preview_url, downloadUrl: artifact.content_url } : {}),
  };
  return element;
}

function buildElement(artifact: ArtifactMeta): AnyElement {
  const url = artifact.content_url;
  const name = artifact.name || artifact.artifact_id;
  const base = { id: artifact.artifact_id, name };

  switch (artifact.render_hint) {
    case "image":
      return { ...base, type: "image", mime: artifact.mime_type ?? undefined, url };

    case "pdf":
      return { ...base, type: "pdf", url };

    case "ifc":
      // IFCViewer loads the raw .ifc in-browser (ThatOpen) from a URL; the content
      // proxy resolves the staged file:// upload and streams it inline.
      return {
        ...base,
        type: "custom",
        name: "IFCViewer",
        url,
        props: { fileUrl: url, filename: artifact.name || "model.ifc" },
      };

    case "mesh":
      return {
        ...base,
        type: "custom",
        name: "MeshViewer",
        // The dataset (manifest, baseUrl, …) comes from the artifact's preview.
        props: { title: artifact.name || "Mesh Viewer" },
      };

    case "mermaid":
      return {
        ...base,
        type: "custom",
        name: "MermaidViewer",
        props: { diagram: mermaidSource(artifact.json_body), title: artifact.name },
      };

    case "json":
      return {
        ...base,
        type: "custom",
        name: "CollapsibleJson",
        props: { data: artifact.json_body, name: artifact.name },
      };

    case "thermal":
      return {
        ...base,
        type: "custom",
        name: "ThermalAnalysisViewer",
        props: {
          name: artifact.name || "thermal_analysis",
          description: artifact.description,
          analysis: artifact.json_body ?? {},
        },
      };

    case "report":
      return {
        ...base,
        type: "custom",
        name: "ReportViewer",
        // Inline body when present; otherwise ReportElement fetches markdown from `url`.
        props: {
          markdown: typeof artifact.json_body === "string" ? artifact.json_body : "",
          title: artifact.name,
          url,
        },
      };

    case "reference":
      return {
        ...base,
        type: "custom",
        name: "ReferenceViewer",
        props: {
          citation: artifact.citation ?? null,
          uri: artifact.uri ?? null,
          name: artifact.name,
          description: artifact.description,
        },
      };

    case "file":
    case "unknown":
    default:
      return { ...base, type: "file", url, props: { description: artifact.description } };
  }
}

/** Convert a turn's artifacts into viewer elements for `ElementList`. */
export function artifactsToElements(artifacts: ArtifactMeta[]): AnyElement[] {
  return artifacts.map(artifactToElement);
}
