import { Badge, Button, Group, Stack, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, ok } from "../api/client";
import { time } from "../lib/format";

export interface UndoResult {
  ok: boolean;
  text: string;
}

const STATE_COLOR: Record<string, string> = {
  applied: "green",
  partially_applied: "green",
  proposed: "blue",
  conflicted: "orange",
  rejected: "red",
  undone: "gray",
};

/** The profile's manual edits, newest first, with Undo for applied ones (TAG-GRID-11). */
export function RecentEdits({
  profile,
  results,
  onUndo,
}: {
  profile: string;
  results: Record<string, UndoResult>;
  onUndo: (changesetId: string, journalId: string) => Promise<void>;
}) {
  const [undoing, setUndoing] = useState<string | null>(null);
  const edits = useQuery({
    queryKey: ["recent-edits", profile],
    queryFn: async () =>
      ok(
        await api.GET("/api/profiles/{profile}/changes", {
          params: { path: { profile }, query: { limit: 20 } },
        }),
      ),
  });
  return (
    <Stack gap="xs" data-testid="recent-edits">
      <Title order={4}>Recent edits</Title>
      {edits.data?.length === 0 && (
        <Text size="sm" c="dimmed">
          No manual edits yet.
        </Text>
      )}
      {edits.data?.map((e) => {
        const changes = e.changeset.targets.flatMap((t) => t.changes);
        const fields = [...new Set(changes.map((c) => c.field))].join(", ");
        const canUndo =
          (e.state === "applied" || e.state === "partially_applied") &&
          e.journal_id != null &&
          e.undo_journal_id == null;
        const result = results[e.changeset.id];
        return (
          <Stack key={e.changeset.id} gap={2} data-testid="recent-edit">
            <Group gap="xs" justify="space-between">
              <Text size="sm">
                {fields}{" "}
                <Text span size="xs" c="dimmed">
                  ({e.changeset.targets.length} target{e.changeset.targets.length === 1 ? "" : "s"})
                </Text>
              </Text>
              <Badge size="sm" variant="light" color={STATE_COLOR[e.state] ?? "gray"}>
                {e.state.replace("_", " ")}
              </Badge>
            </Group>
            <Group gap="xs" justify="space-between">
              <Text size="xs" c="dimmed">
                {time(e.changeset.created_at)}
              </Text>
              {canUndo && e.journal_id != null && (
                <Button
                  size="compact-xs"
                  variant="subtle"
                  loading={undoing === e.changeset.id}
                  onClick={() => {
                    const journal = e.journal_id;
                    if (journal == null) return;
                    setUndoing(e.changeset.id);
                    void onUndo(e.changeset.id, journal).finally(() => {
                      setUndoing(null);
                    });
                  }}
                >
                  Undo
                </Button>
              )}
            </Group>
            {result && (
              <Text size="xs" c={result.ok ? "dimmed" : "red"} data-testid="undo-result">
                {result.text}
              </Text>
            )}
          </Stack>
        );
      })}
    </Stack>
  );
}
