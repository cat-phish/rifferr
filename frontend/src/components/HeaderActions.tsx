import { Group } from "@mantine/core";
import { type ReactNode, createContext, useContext } from "react";
import { createPortal } from "react-dom";

/** The element in a queue item's header row that holds its main actions (UI-REFRESH-10). */
export const ActionsSlotContext = createContext<HTMLElement | null>(null);

/**
 * A queue item's main actions. Inside a review queue item they render in the item's
 * header row, on the right; anywhere else they render in place.
 */
export function HeaderActions({ children }: { children: ReactNode }) {
  const slot = useContext(ActionsSlotContext);
  const group = (
    <Group gap="xs" wrap="wrap" justify="flex-end">
      {children}
    </Group>
  );
  return slot ? createPortal(group, slot) : group;
}
