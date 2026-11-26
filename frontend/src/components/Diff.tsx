import { Text } from "@mantine/core";
import { useMemo } from "react";
import { type DiffSegment, diffText } from "../lib/diff";
import { tc } from "../theme";

const REMOVED = { color: tc("diffRemoved"), textDecoration: "line-through" } as const;
const ADDED = { color: tc("diffAdded"), fontWeight: 600 } as const;

/**
 * Old → new value as a diff (UI-REFRESH-4): unchanged text plain, removed parts red
 * and struck through, added parts green. Text nodes only (REVIEW-QUEUE-17).
 * An unchanged value is dimmed (UI-REFRESH-5).
 *
 * Values that are mostly different show as `old → new` instead of an inline diff.
 *
 * `side` picks what to show: `both` (one cell, removed then added), or `old` / `new`
 * for side-by-side columns, where each shows its own value with its own changes marked.
 */
/** At least 60% of the longer value, and 3 characters, unchanged (REVIEW-4). */
export function mostlySame(segs: DiffSegment[], old: string, next: string): boolean {
  const same = segs.filter((s) => s.kind === "same").reduce((n, s) => n + s.text.trim().length, 0);
  return same >= 3 && same >= 0.6 * Math.max(old.length, next.length);
}

export function Diff({
  old,
  new: next,
  side = "both",
  fz = "sm",
  empty = "(empty)",
}: {
  old: string;
  new: string;
  side?: "both" | "old" | "new";
  fz?: string;
  empty?: string;
}) {
  const segs = useMemo(() => diffText(old, next), [old, next]);
  if (old === next) {
    return (
      <Text span fz={fz} c="dimmed" data-diff="same">
        {old || empty}
      </Text>
    );
  }
  const showRemoved = side !== "new";
  const showAdded = side !== "old";
  // Values that are mostly different (a number, a date, a new name) show apart as
  // old → new, so they don't read as one (REVIEW-4). The inline diff is kept for a small
  // edit inside a value that is mostly the same.
  if (side === "both" && !mostlySame(segs, old, next)) {
    return (
      <Text span fz={fz} data-diff="replaced">
        <span style={REMOVED} data-seg="removed">
          {old || empty}
        </span>
        <span style={{ color: "var(--mantine-color-dimmed)" }} aria-label="becomes">
          {" → "}
        </span>
        <span style={ADDED} data-seg="added">
          {next || empty}
        </span>
      </Text>
    );
  }
  return (
    <Text span fz={fz} data-diff="changed">
      {showRemoved && old === "" && (
        <span style={REMOVED} data-seg="removed">
          {empty}
        </span>
      )}
      {segs.map((s, i) => {
        if (s.kind === "same") return <span key={i}>{s.text}</span>;
        if (s.kind === "removed" && !showRemoved) return null;
        if (s.kind === "added" && !showAdded) return null;
        return (
          <span key={i} style={s.kind === "removed" ? REMOVED : ADDED} data-seg={s.kind}>
            {s.text}
          </span>
        );
      })}
      {showAdded && next === "" && (
        <span style={ADDED} data-seg="added">
          {side === "both" ? ` ${empty}` : empty}
        </span>
      )}
    </Text>
  );
}
