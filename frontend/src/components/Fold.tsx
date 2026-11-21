import { Button } from "@mantine/core";
import { type ReactNode, useState } from "react";

/** Tables longer than this show their first rows and "Show all N" (UI-REFRESH-12). */
export const FOLD_LIMIT = 8;

/**
 * Fold a long list: returns the rows to render and the control (null when the
 * list is short enough).
 */
export function useFold<T>(
  rows: readonly T[],
  limit = FOLD_LIMIT,
): { shown: readonly T[]; control: ReactNode } {
  const [open, setOpen] = useState(false);
  if (rows.length <= limit) return { shown: rows, control: null };
  return {
    shown: open ? rows : rows.slice(0, limit),
    control: (
      <Button
        variant="subtle"
        size="compact-xs"
        onClick={() => {
          setOpen((o) => !o);
        }}
      >
        {open ? "Show fewer" : `Show all ${String(rows.length)}`}
      </Button>
    ),
  };
}
