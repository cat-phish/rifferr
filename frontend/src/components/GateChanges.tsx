import { Badge, Group, Table, Text, Tooltip } from "@mantine/core";
import { useMemo } from "react";
import type { GateAlbum } from "../api/types";
import { groupChanges } from "../lib/gateChanges";
import { formatValue } from "../lib/review";
import { Diff } from "./Diff";
import { useFold } from "./Fold";

/** Lined, striped rows for a gate's tables (REVIEW-4): field names line up with values. */
export const GATE_TABLE = {
  fz: "sm",
  striped: true,
  withRowBorders: true,
  verticalSpacing: 6,
  horizontalSpacing: "sm",
} as const;

/** The run's changes to an album so far, grouped: one row per field, value and steps,
 * naming the tracks it touched and the steps that made it (S-REVIEW REVIEW-4). */
export function GateChanges({ album }: { album: GateAlbum }) {
  const rows = useMemo(() => groupChanges(album), [album]);
  const fold = useFold(rows);
  if (rows.length === 0) {
    return (
      <Text size="xs" c="dimmed">
        No tag changes by this run so far.
      </Text>
    );
  }
  return (
    <>
      <Table {...GATE_TABLE} data-testid="gate-changes">
        <Table.Thead>
          <Table.Tr>
            <Table.Th w={180}>Changed by this run</Table.Th>
            <Table.Th>Where</Table.Th>
            <Table.Th>Before → after</Table.Th>
            <Table.Th>By step</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {fold.shown.map((r) => (
            <Table.Tr key={r.key} data-testid="gate-change-row">
              <Table.Td ff="monospace">{r.field}</Table.Td>
              <Table.Td data-testid="gate-change-where">
                {r.tracks.length > 1 ? (
                  <Tooltip label={r.tracks.join("\n")} style={{ whiteSpace: "pre-line" }}>
                    <Text span fz="sm" td="underline dotted">
                      {r.where}
                    </Text>
                  </Tooltip>
                ) : (
                  r.where
                )}
              </Table.Td>
              <Table.Td data-testid="gate-change">
                <Diff
                  old={r.existedBefore ? formatValue(r.before) : ""}
                  new={formatValue(r.after)}
                  empty="—"
                />
              </Table.Td>
              <Table.Td>
                <Group gap={4}>
                  {r.steps.map((s) => (
                    <Badge
                      key={s}
                      size="sm"
                      variant="light"
                      tt="none"
                      data-testid="gate-change-step"
                    >
                      {s}
                    </Badge>
                  ))}
                </Group>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      {fold.control}
    </>
  );
}
