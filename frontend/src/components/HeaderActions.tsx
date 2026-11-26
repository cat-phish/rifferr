import { Group } from "@mantine/core";
import { type ReactNode, createContext, useContext } from "react";
import { createPortal } from "react-dom";

/** The element in a queue item's header row that holds its main actions (UI-REFRESH-10). */
export const ActionsSlotContext = createContext<HTMLElement | null>(null);

/** The same actions again at the bottom of a tall queue item, so a long review needn't be
 * scrolled back up to decide (REVIEW-QUEUE-20). Null on short items. */
export const FooterSlotContext = createContext<HTMLElement | null>(null);

/**
 * A queue item's main actions. Inside a review queue item they render in the item's
 * header row, on the right; anywhere else they render in place.
 */
export function HeaderActions({ children }: { children: ReactNode }) {
  const slot = useContext(ActionsSlotContext);
  const footer = useContext(FooterSlotContext);
  const group = (
    <Group gap="xs" wrap="wrap" justify="flex-end">
      {children}
    </Group>
  );
  if (!slot) return group;
  return (
    <>
      {createPortal(group, slot)}
      {footer && createPortal(group, footer)}
    </>
  );
}
