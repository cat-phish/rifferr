import { describe, expect, it } from "vitest";
import type { RankedCandidate } from "../api/types";
import { imagesFrom, providerRows, rankCandidates, scoreWith } from "./covers";
import rankCase from "./rank-case.json";

interface CaseCandidate {
  provider: string;
  image_url: string;
  width?: number;
  height?: number;
  match_confidence: number;
}

/** What the server sends for a candidate (COVER-PICKER-8), computed from the case. */
function ranked(c: CaseCandidate): RankedCandidate {
  const s = rankCase.settings;
  const known = c.width !== undefined && c.height !== undefined;
  const size = known
    ? Math.min(1, Math.min(c.width ?? 0, c.height ?? 0) / s.target_size)
    : s.unknown_size_score;
  let filtered: "small" | "aspect" | null = null;
  if (known && Math.min(c.width ?? 0, c.height ?? 0) < s.min_size) filtered = "small";
  else if (known && Math.abs((c.width ?? 0) / (c.height ?? 1) - 1) > s.max_aspect_error)
    filtered = "aspect";
  const weight = rankCase.providers.find((p) => p.name === c.provider)?.weight ?? 1;
  return {
    id: c.image_url,
    provider: c.provider,
    image_url: c.image_url,
    width: c.width ?? null,
    height: c.height ?? null,
    match_confidence: c.match_confidence,
    score: weight * c.match_confidence * size,
    size_score: size,
    filtered,
  };
}

const candidates = (rankCase.candidates as unknown as CaseCandidate[]).map(ranked);

describe("COVER-PICKER-8 ranking", () => {
  it("COVER-PICKER-8 matches the server's order for the shared case", () => {
    const order = rankCandidates(candidates, rankCase.providers).map((r) => r.candidate.id);
    expect(order).toEqual(rankCase.expected);
    const all = rankCandidates(candidates, rankCase.providers, true).map((r) => r.candidate.id);
    expect(all.filter((id) => !order.includes(id)).sort()).toEqual([...rankCase.hidden].sort());
  });

  it("COVER-PICKER-9 re-ranks with new weights and hides disabled providers", () => {
    const providers = rankCase.providers.map((p) =>
      p.name === "deezer"
        ? { ...p, weight: 10 }
        : p.name === "coverartarchive"
          ? { ...p, enabled: false }
          : p,
    );
    const order = rankCandidates(candidates, providers).map((r) => r.candidate.id);
    expect(order[0]).toBe("https://x/d1000");
    expect(order).not.toContain("https://x/caa");
  });

  it("COVER-PICKER-8 keeps a stored score without size_score", () => {
    const old = { ...(candidates[0] as RankedCandidate), size_score: null, score: 0.42 };
    expect(scoreWith(old, 5)).toBe(0.42);
  });

  it("COVER-PICKER-9 lists installed providers the profile doesn't, off", () => {
    expect(
      providerRows([{ name: "deezer", enabled: true, weight: 2 }], ["deezer", "local", "upload"]),
    ).toEqual([
      { name: "deezer", enabled: true, weight: 2 },
      { name: "local", enabled: false, weight: 1 },
    ]);
  });
});

describe("COVER-PICKER-17, -19 clipboard", () => {
  function transfer(files: File[], types: string[]): DataTransfer {
    return {
      files: files as unknown as FileList,
      items: [] as unknown as DataTransferItemList,
      types,
    } as unknown as DataTransfer;
  }

  it("COVER-PICKER-17 takes image files", () => {
    const png = new File([new Uint8Array([1])], "a.png", { type: "image/png" });
    expect(imagesFrom(transfer([png], ["Files"]))).toEqual({ images: [png], text: false });
  });

  it("COVER-PICKER-19 a pasted link is text, never fetched", () => {
    expect(imagesFrom(transfer([], ["text/plain", "text/uri-list"]))).toEqual({
      images: [],
      text: true,
    });
  });
});
