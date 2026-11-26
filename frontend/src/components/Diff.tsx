import { Text } from "@mantine/core";
import { useMemo } from "react";
import { diffText } from "../lib/diff";
import { tc } from "../theme";

const REMOVED = { color: tc("diffRemoved"), textDecoration: "line-through" } as const;
const ADDED = { color: tc("diffAdded"), fontWeight: 600 } as const;

/**
 * Old → new value as a diff (UI-REFRESH-4): unchanged text plain, removed parts red
 * and struck through, added parts green. Text nodes only (REVIEW-QUEUE-17).
 * An unchanged value is dimmed (UI-REFRESH-5).
 *
 * In one cell (`side="both"`) a change always shows as `old → new`, the whole old value
 * struck through (REVIEW-4, amended 2026-11-25): no inline character diff.
 *
 * `side` picks what to show: `both` (one cell, old → new), or `old` / `new` for
 * side-by-side columns, where each shows its own value with its own changes marked.
 */
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
  if (side === "both") {
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
  const showRemoved = side === "old";
  const showAdded = side === "new";
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
          {empty}
        </span>
      )}
    </Text>
  );
}
