import { Alert, Badge, Button, Group, Stack, Table, Text, TextInput } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ok } from "../api/client";
import type { CleanupDecision, CleanupEntry } from "../api/types";
import { time } from "../lib/format";

type Choice = "delete" | "keep";
type Cls = CleanupEntry["cls"];

/** The listing's classes in the order shown, with what each means (FILES-CLEANUP-8). */
export const CLASSES: { cls: Cls; label: string; says: string; color: string }[] = [
  { cls: "delete", label: "Will be deleted", says: "A delete pattern matches.", color: "red" },
  { cls: "empty", label: "Empty folders", says: "Removed once empty.", color: "orange" },
  { cls: "left", label: "Left", says: "No pattern deletes these; check them.", color: "yellow" },
  { cls: "keep", label: "Kept", says: "A keep pattern matches.", color: "gray" },
  {
    cls: "in_library",
    label: "In the library",
    says: "beets items: never deleted.",
    color: "gray",
  },
  { cls: "busy", label: "Busy", says: "Other work still needs these.", color: "gray" },
];

/** ``1536`` -> ``"1.5 KB"``. */
export function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "";
  if (n < 1024) return `${String(n)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit] ?? "TB"}`;
}

/** Entries by class, in ``CLASSES`` order; classes without entries are left out. */
export function byClass(entries: CleanupEntry[]): { cls: Cls; entries: CleanupEntry[] }[] {
  return CLASSES.map(({ cls }) => ({ cls, entries: entries.filter((e) => e.cls === cls) })).filter(
    (g) => g.entries.length > 0,
  );
}

/** A cleanup decision in the review queue (S-FILES-CLEANUP FILES-CLEANUP-14). */
export function CleanupDecisionView({ decision }: { decision: CleanupDecision }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const decide = useMutation({
    mutationFn: async (choice: Choice) =>
      ok(
        await api.POST("/api/cleanups/{decision_id}/decision", {
          params: { path: { decision_id: decision.id } },
          body: { decision: choice, note: note.trim() === "" ? null : note.trim() },
        }),
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["review"] }),
  });
  const groups = byClass(decision.entries);
  const deleting = decision.entries.filter((e) => e.cls === "delete" || e.cls === "empty").length;
  const options = decision.options;

  return (
    <Stack gap="sm" data-testid="cleanup-decision">
      <Text size="sm" ff="monospace" data-testid="cleanup-path">
        {decision.path}
      </Text>
      <Text size="sm" c="dimmed">
        {options.trash ? `Files go to the trash folder ${options.trash}.` : "Files are deleted."}{" "}
        Only entries listed here as deleted are removed, and only if they haven't changed since.
      </Text>
      {decision.deadline && (
        <Text size="sm" data-testid="deadline">
          If nobody decides by {time(decision.deadline)}, it will{" "}
          {decision.on_timeout === "delete" ? "delete them" : "keep everything"} automatically.
        </Text>
      )}
      {groups.map(({ cls, entries }) => {
        const meta = CLASSES.find((c) => c.cls === cls);
        return (
          <Stack key={cls} gap={4} data-testid="cleanup-group" data-cls={cls}>
            <Group gap="xs">
              <Badge color={meta?.color} variant="light">
                {meta?.label ?? cls} ({String(entries.length)})
              </Badge>
              <Text size="xs" c="dimmed">
                {meta?.says}
              </Text>
            </Group>
            <Table withRowBorders={false} verticalSpacing={2}>
              <Table.Tbody>
                {entries.map((e) => (
                  <Table.Tr key={e.path}>
                    <Table.Td ff="monospace" fz="sm">
                      {e.kind === "dir" ? `${e.path}/` : e.path}
                    </Table.Td>
                    <Table.Td fz="sm" c="dimmed">
                      {bytes(e.size)}
                    </Table.Td>
                    <Table.Td fz="sm" c="dimmed">
                      {e.reason ?? ""}
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Stack>
        );
      })}
      {decision.truncated > 0 && (
        <Text size="sm" c="dimmed" data-testid="cleanup-truncated">
          {String(decision.truncated)} more entries aren't shown; they will be left.
        </Text>
      )}
      {decide.error && <Alert color="red">{decide.error.message}</Alert>}
      {decide.isSuccess ? (
        <Text size="sm" c="dimmed" data-testid="decision-sent">
          {decide.variables === "keep" ? "Keeping everything." : "Cleaning the folder."}
        </Text>
      ) : (
        <Group justify="space-between" align="flex-end">
          <TextInput
            size="xs"
            label="Note (optional)"
            value={note}
            onChange={(e) => {
              setNote(e.currentTarget.value);
            }}
            w={260}
          />
          <Group gap="xs">
            <Button
              size="xs"
              color="red"
              disabled={decide.isPending || deleting === 0}
              loading={decide.isPending && decide.variables === "delete"}
              onClick={() => {
                decide.mutate("delete");
              }}
            >
              Delete
            </Button>
            <Button
              size="xs"
              variant="light"
              disabled={decide.isPending}
              loading={decide.isPending && decide.variables === "keep"}
              onClick={() => {
                decide.mutate("keep");
              }}
            >
              Keep everything
            </Button>
          </Group>
        </Group>
      )}
    </Stack>
  );
}
