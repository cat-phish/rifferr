import { List, Text } from "@mantine/core";
import type { SafetyFinding } from "../api/types";

/** The static safety report (SECURITY-11), shown in full, also when empty (UI-SHELL-4). */
export function SafetyReport({ findings }: { findings: SafetyFinding[] }) {
  if (findings.length === 0) {
    return <Text size="sm">No findings: this workflow runs no commands and reads no secrets.</Text>;
  }
  return (
    <List size="sm" spacing={4} data-testid="safety">
      {findings.map((f, i) => (
        <List.Item key={`${f.step_id}-${String(i)}`}>
          <Text span fw={600}>
            [{f.step_id}] {f.kind}
          </Text>
          :{" "}
          <Text span ff="monospace">
            {f.detail}
          </Text>
        </List.Item>
      ))}
    </List>
  );
}
