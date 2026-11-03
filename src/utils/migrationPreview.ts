export function attachmentFilename(disposition: string | null): string | null {
  return /^attachment;\s*filename="([A-Za-z0-9_.-]+\.php)"$/i.exec(disposition || '')?.[1] ?? null;
}
