type ArtifactFileElement = {
  name?: string;
  props?: Record<string, unknown>;
};

export function artifactFilePresentation(element: ArtifactFileElement): {
  label: string;
  filename: string;
} {
  const filename = element.name?.trim() || "Attachment";
  const description = element.props?.description;
  const label =
    typeof description === "string" && description.trim()
      ? description.trim()
      : filename;

  return { label, filename };
}
