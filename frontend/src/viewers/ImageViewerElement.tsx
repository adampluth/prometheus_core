import { FileDown } from "lucide-react";
import type { ViewerProps } from "@/lib/elements/elements";

function stringProp(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function svgDataUrl(svg: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

export function ImageViewerElement({ element }: ViewerProps) {
  const props = element.props ?? {};
  const svgContent = stringProp(props.svgContent);
  const svgUrl = stringProp(props.svgUrl);
  const name =
    stringProp(props.name) ||
    element.name ||
    (element.mime === "image/svg+xml" ? "image.svg" : "image");

  // Inline SVG > explicit SVG url > the artifact content proxy url set by the
  // adapter (the artifact's `content_url`).
  const url = (svgContent ? svgDataUrl(svgContent) : "") || svgUrl || element.url || null;

  if (!url) return null;

  return (
    <figure className="border border-base-300 rounded-lg overflow-hidden my-2 bg-base-200/40 max-w-3xl">
      <div className="flex items-center justify-between gap-3 px-3 py-2 border-b border-base-300 bg-base-200/60">
        <figcaption className="text-sm font-medium text-base-content truncate">{name}</figcaption>
        <a
          href={url}
          download={name}
          className="btn btn-ghost btn-xs shrink-0"
          aria-label={`Download ${name}`}
        >
          <FileDown size={13} />
          Download
        </a>
      </div>
      <div className="flex justify-center bg-base-100 p-2">
        <img
          src={url}
          alt={name}
          loading="lazy"
          decoding="async"
          className="max-h-[70vh] max-w-full rounded object-contain"
        />
      </div>
    </figure>
  );
}
