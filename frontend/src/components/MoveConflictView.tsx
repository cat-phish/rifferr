import { Alert, Button, Group, List, Stack, Text } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ok } from "../api/client";
import type { MoveConflict } from "../api/types";

type Choice = "retry" | "fail";

/** An album that can't move yet, in the review queue (S-LIBRARY-MOVE LIBRARY-MOVE-5a).
 * "Retry" plans it again once the conflict is fixed and moves it at once if nothing
 * conflicts any more; "Fail" ends the album's lane. */
export function MoveConflictView({ conflict }: { conflict: MoveConflict }) {
  const queryClient = useQueryClient();
  const decide = useMutation({
    mutationFn: async (choice: Choice) =>
      ok(
        await api.POST("/api/move-conflicts/{conflict_id}/decision", {
          params: { path: { conflict_id: conflict.id } },
          body: { decision: choice },
        }),
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["review"] }),
  });
  const verb = conflict.settings.mode === "copy" ? "copied" : "moved";

  return (
    <Stack gap="sm" data-testid="move-conflict">
      <Text size="sm">{conflict.label}</Text>
      {conflict.src_dir && (
        <Text size="sm" ff="monospace" data-testid="move-folders">
          {conflict.src_dir} → {conflict.dst_dir}
        </Text>
      )}
      <Text size="sm" c="dimmed">
        Nothing was {verb}. Rifferr never overwrites or merges folders: fix what's listed, then
        retry.
        {conflict.attempts > 0 && ` Retried ${String(conflict.attempts)}×, still conflicting.`}
      </Text>
      <List size="sm" data-testid="move-conflicts">
        {conflict.conflicts.map((line) => (
          <List.Item key={line} ff="monospace">
            {line}
          </List.Item>
        ))}
      </List>
      {decide.error && <Alert color="red">{decide.error.message}</Alert>}
      {decide.isSuccess ? (
        <Text size="sm" c="dimmed" data-testid="decision-sent">
          {decide.variables === "retry" ? "Retrying the move." : "The album's lane failed."}
        </Text>
      ) : (
        <Group gap="xs" justify="flex-end">
          <Button
            size="xs"
            disabled={decide.isPending}
            loading={decide.isPending && decide.variables === "retry"}
            onClick={() => {
              decide.mutate("retry");
            }}
          >
            Retry
          </Button>
          <Button
            size="xs"
            variant="light"
            color="red"
            disabled={decide.isPending}
            loading={decide.isPending && decide.variables === "fail"}
            onClick={() => {
              decide.mutate("fail");
            }}
          >
            Fail album
          </Button>
        </Group>
      )}
    </Stack>
  );
}
