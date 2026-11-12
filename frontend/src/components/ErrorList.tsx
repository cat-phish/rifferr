import { List, Text } from "@mantine/core";
import type { WorkflowError } from "../api/types";

/** Validation errors with line and column (UI-SHELL-3). */
export function ErrorList({ errors }: { errors: WorkflowError[] }) {
  return (
    <List size="sm" spacing={4} data-testid="errors">
      {errors.map((e, i) => {
        const pos = [e.line, e.column].filter((x) => x !== null && x !== undefined).join(":");
        return (
          <List.Item key={i}>
            <Text span ff="monospace">
              {pos ? `${pos}: ` : ""}
              {e.path ? `${e.path}: ` : ""}
            </Text>
            {e.message}
          </List.Item>
        );
      })}
    </List>
  );
}
