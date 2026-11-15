import type { RankedCandidate } from "../api/types";

/** The provider of images the user added (COVER-PICKER-18); never searched. */
export const UPLOAD_PROVIDER = "upload";

/** A provider as the picker's panel edits it (COVER-PICKER-9). */
export interface ProviderRow {
  name: string;
  enabled: boolean;
  weight: number;
}

/** A candidate's image through the server, which only serves candidates it found or was
 * given (COVERS-SEARCH-20). Never the provider's own URL. */
export function candidateImage(profile: string, id: string, thumb = true): string {
  const params = new URLSearchParams({ profile });
  if (thumb) params.set("thumb", "1");
  return `/api/covers/image/${encodeURIComponent(id)}?${params.toString()}`;
}

/** The current art of a library album (COVERS-SEARCH-22). */
export function albumArt(profile: string, albumId: string): string {
  return `/api/albums/${encodeURIComponent(albumId)}/art?${new URLSearchParams({ profile }).toString()}`;
}

export function sizeLabel(c: { width?: number | null; height?: number | null }): string {
  return c.width != null && c.height != null
    ? `${String(c.width)}×${String(c.height)}`
    : "size unknown";
}

export function bytesLabel(n: number): string {
  if (n < 1000) return `${String(n)} B`;
  if (n < 1_000_000) return `${(n / 1000).toFixed(0)} KB`;
  return `${(n / 1_000_000).toFixed(1)} MB`;
}

export const FILTER_LABEL: Record<string, string> = { small: "too small", aspect: "wrong shape" };

/** Score with the panel's weights: ``weight × match_confidence × size_score``, the same
 * formula as the server (COVERS-SEARCH-7, COVER-PICKER-8). A candidate stored before
 * ``size_score`` existed keeps its stored score. */
export function scoreWith(c: RankedCandidate, weight: number): number {
  if (c.size_score == null) return c.score;
  return Math.round(weight * c.match_confidence * c.size_score * 10000) / 10000;
}

export interface Ranked {
  candidate: RankedCandidate;
  score: number;
}

/** The candidates to show, best first: those of enabled providers (and the user's own),
 * hidden ones only with ``showHidden``. Ties break by the panel's provider order, then by
 * the larger area, as on the server. */
export function rankCandidates(
  candidates: Iterable<RankedCandidate>,
  providers: ProviderRow[],
  showHidden = false,
): Ranked[] {
  const position = new Map(providers.map((p, i) => [p.name, i]));
  const byName = new Map(providers.map((p) => [p.name, p]));
  const out: Ranked[] = [];
  const seen = new Set<string>();
  for (const c of candidates) {
    if (seen.has(c.id)) continue;
    seen.add(c.id);
    const row = byName.get(c.provider);
    if (c.provider !== UPLOAD_PROVIDER && row !== undefined && !row.enabled) continue;
    if (c.filtered != null && !showHidden) continue;
    out.push({ candidate: c, score: scoreWith(c, row?.weight ?? 1) });
  }
  const area = (c: RankedCandidate) => (c.width ?? 0) * (c.height ?? 0);
  return out.sort(
    (a, b) =>
      b.score - a.score ||
      (position.get(a.candidate.provider) ?? position.size) -
        (position.get(b.candidate.provider) ?? position.size) ||
      area(b.candidate) - area(a.candidate),
  );
}

/** The panel's rows: the profile's providers in order, then every other installed one,
 * off (COVER-PICKER-9). */
export function providerRows(listed: ProviderRow[], registered: string[]): ProviderRow[] {
  const names = new Set(listed.map((p) => p.name));
  return [
    ...listed.filter((p) => p.name !== UPLOAD_PROVIDER),
    ...registered
      .filter((n) => !names.has(n) && n !== UPLOAD_PROVIDER)
      .map((name) => ({ name, enabled: false, weight: 1 })),
  ];
}

/** An image file or clipboard blob as base64, without the data URL prefix. */
export async function toBase64(blob: Blob): Promise<string> {
  const buffer = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < buffer.length; i += chunk) {
    binary += String.fromCharCode(...buffer.subarray(i, i + chunk));
  }
  return btoa(binary);
}

export const IMAGE_TYPES = ["image/png", "image/jpeg"] as const;

/** The images a paste or drop carries; ``text`` is set when it carries only text, such
 * as a link (COVER-PICKER-19). */
export function imagesFrom(data: DataTransfer | null): { images: File[]; text: boolean } {
  if (data === null) return { images: [], text: false };
  const images = [...data.files].filter((f) => f.type.startsWith("image/"));
  if (images.length === 0) {
    for (const item of [...data.items]) {
      if (item.kind === "file" && item.type.startsWith("image/")) {
        const f = item.getAsFile();
        if (f !== null) images.push(f);
      }
    }
  }
  const text = images.length === 0 && [...data.types].some((t) => t.startsWith("text/"));
  return { images, text };
}

export const PASTE_LINK_MESSAGE = "Paste an image, not a link: copy the image itself.";
