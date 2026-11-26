/** Old → new text diff for review screens (F-UI-REFRESH UI-REFRESH-4). */

export type DiffSegment = { kind: "same" | "removed" | "added"; text: string };

/** Above this many cells the LCS table is skipped and the value is shown as replaced. */
const MAX_CELLS = 250_000;

function lcsDiff(a: string[], b: string[]): DiffSegment[] {
  const n = a.length;
  const m = b.length;
  if (n * m > MAX_CELLS) {
    return [
      { kind: "removed", text: a.join("") },
      { kind: "added", text: b.join("") },
    ];
  }
  // dp[i][j] = LCS length of a[i:] and b[j:]
  const dp: Uint32Array[] = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    const row = dp[i] as Uint32Array;
    const next = dp[i + 1] as Uint32Array;
    for (let j = m - 1; j >= 0; j--) {
      row[j] =
        a[i] === b[j]
          ? (next[j + 1] as number) + 1
          : Math.max(next[j] as number, row[j + 1] as number);
    }
  }
  const out: DiffSegment[] = [];
  const push = (kind: DiffSegment["kind"], text: string) => {
    const last = out[out.length - 1];
    if (last && last.kind === kind) last.text += text;
    else out.push({ kind, text });
  };
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      push("same", a[i] as string);
      i++;
      j++;
    } else if (
      ((dp[i + 1] as Uint32Array)[j] as number) >= ((dp[i] as Uint32Array)[j + 1] as number)
    ) {
      push("removed", a[i] as string);
      i++;
    } else {
      push("added", b[j] as string);
      j++;
    }
  }
  while (i < n) push("removed", a[i++] as string);
  while (j < m) push("added", b[j++] as string);
  return reorder(out);
}

/** Within each run of changes, show what is removed before what is added. */
function reorder(segs: DiffSegment[]): DiffSegment[] {
  const out: DiffSegment[] = [];
  let removed = "";
  let added = "";
  const flush = () => {
    if (removed) out.push({ kind: "removed", text: removed });
    if (added) out.push({ kind: "added", text: added });
    removed = "";
    added = "";
  };
  for (const s of segs) {
    if (s.kind === "same") {
      flush();
      out.push(s);
    } else if (s.kind === "removed") removed += s.text;
    else added += s.text;
  }
  flush();
  return out;
}

/**
 * Words: runs of letters and digits (apostrophes inside a word included), each run of
 * whitespace, and every other character on its own, so `/`, `-` and `(` are their own
 * tokens and a path diffs by folder name.
 */
function words(s: string): string[] {
  return s.match(/[\p{L}\p{M}\p{N}]+(?:['’][\p{L}\p{M}\p{N}]+)*|\s+|./gsu) ?? [];
}

/**
 * Diff two values by whole words (author, 2026-11-25): a changed word or folder name
 * is marked whole, never letter by letter.
 */
export function diffText(oldText: string, newText: string): DiffSegment[] {
  if (oldText === newText) return oldText ? [{ kind: "same", text: oldText }] : [];
  return lcsDiff(words(oldText), words(newText));
}

/** The old value back from a diff (every segment but the added ones). */
export function oldOf(segs: DiffSegment[]): string {
  return segs
    .filter((s) => s.kind !== "added")
    .map((s) => s.text)
    .join("");
}

/** The new value back from a diff (every segment but the removed ones). */
export function newOf(segs: DiffSegment[]): string {
  return segs
    .filter((s) => s.kind !== "removed")
    .map((s) => s.text)
    .join("");
}
