import { Alert, Badge, Button, Group, Stack, Table, Text } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ok } from "../api/client";
import type { PruneAlbum, PruneDecision } from "../api/types";
import { jobOf, useMarkDecided } from "./Decided";
import { HeaderActions } from "./HeaderActions";
import { NoteField } from "./NoteField";

function Albums({ albums, testId }: { albums: PruneAlbum[]; testId: string }) {
  return (
    <Table withRowBorders={false} verticalSpacing={2}>
      <Table.Tbody>
        {albums.map((a) => (
          <Table.Tr key={a.album_id} data-testid={testId}>
            <Table.Td fz="sm">{a.label}</Table.Td>
            <Table.Td fz="sm" ff="monospace" c="dimmed">
              {a.path ?? ""}
            </Table.Td>
            <Table.Td fz="sm" c="dimmed">
              {a.missing === a.items
                ? `${String(a.items)} files, all gone`
                : `${String(a.missing)} of ${String(a.items)} files gone`}
            </Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
}

/** Library entries whose files are gone, waiting for Accept or Reject
 * (S-LIBRARY-PRUNE LIBRARY-PRUNE-3). */
export function PruneDecisionView({ decision }: { decision: PruneDecision }) {
  const queryClient = useQueryClient();
  const markDecided = useMarkDecided();
  const [note, setNote] = useState("");
  const decide = useMutation({
    mutationFn: async (choice: "accept" | "reject") =>
      ok(
        await api.POST("/api/prunes/{decision_id}/decision", {
          params: { path: { decision_id: decision.id } },
          body: { decision: choice, note: note.trim() === "" ? null : note.trim() },
        }),
      ),
    onSuccess: (res) => {
      markDecided(jobOf(res));
      void queryClient.invalidateQueries({ queryKey: ["review"] });
    },
  });
  const partial = decision.partial ?? [];
  return (
    <Stack gap="sm" data-testid="prune-decision">
      <Text size="sm" c="dimmed">
        These albums are in the library but their files are gone. Accepting drops their library
        entries; no file is touched.
      </Text>
      <Group gap="xs">
        <Badge color="red" variant="light">
          To drop ({String(decision.albums.length)})
        </Badge>
      </Group>
      <Albums albums={decision.albums} testId="prune-album" />
      {partial.length > 0 && (
        <>
          <Group gap="xs">
            <Badge color="yellow" variant="light">
              Partly gone, kept ({String(partial.length)})
            </Badge>
            <Text size="xs" c="dimmed">
              Some files are still there; check them by hand.
            </Text>
          </Group>
          <Albums albums={partial} testId="prune-partial" />
        </>
      )}
      {decide.error && <Alert color="red">{decide.error.message}</Alert>}
      <NoteField value={note} onChange={setNote} disabled={decide.isPending} />
      <HeaderActions>
        <Button
          size="xs"
          color="red"
          disabled={decide.isPending}
          loading={decide.isPending && decide.variables === "accept"}
          onClick={() => {
            decide.mutate("accept");
          }}
        >
          Drop {String(decision.albums.length)}
        </Button>
        <Button
          size="xs"
          variant="light"
          disabled={decide.isPending}
          loading={decide.isPending && decide.variables === "reject"}
          onClick={() => {
            decide.mutate("reject");
          }}
        >
          Keep all
        </Button>
      </HeaderActions>
    </Stack>
  );
}
