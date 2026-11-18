import { Alert, Badge, Button, Group, Stack, Table, Text, TextInput } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ok } from "../api/client";
import type { ChangeSet } from "../api/types";

type FileEffect = NonNullable<ChangeSet["files"]>[number];

function dirname(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut <= 0 ? "/" : path.slice(0, cut);
}

/** The deepest folder holding every one of ``paths`` (files). */
export function commonDir(paths: string[]): string {
  const dirs = paths.map(dirname);
  if (dirs.length === 0) return "";
  let parts = (dirs[0] ?? "").split("/");
  for (const d of dirs.slice(1)) {
    const other = d.split("/");
    let n = 0;
    while (n < parts.length && n < other.length && parts[n] === other[n]) n += 1;
    parts = parts.slice(0, n);
  }
  return parts.join("/") || "/";
}

/** ``path`` relative to ``dir``, or the whole path when it isn't below it. */
export function relative(path: string, dir: string): string {
  const prefix = dir.endsWith("/") ? dir : `${dir}/`;
  return path.startsWith(prefix) ? path.slice(prefix.length) : path;
}

/** Old and new album folder: those of the tracked files (items and art), so artifacts
 * in sub-folders don't shift them. */
export function folders(files: FileEffect[]): { from: string; to: string } {
  const tracked = files.filter((f) => f.kind !== "artifact");
  const use = tracked.length > 0 ? tracked : files;
  return { from: commonDir(use.map((f) => f.src)), to: commonDir(use.map((f) => f.dst)) };
}

const KIND_SAYS: Record<FileEffect["kind"], string> = {
  item: "track",
  art: "cover",
  artifact: "artifact",
};

/** A FileChangeSet in the review queue (S-LIBRARY-MOVE LIBRARY-MOVE-16): the album's old
 * and new folder and each file's old and new name. It is accepted or rejected as a whole,
 * so an album never ends up split across folders. */
export function FileChangeSetView({ id, changeset }: { id: string; changeset: ChangeSet }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const decide = useMutation({
    mutationFn: async (decision: "accept" | "reject") =>
      ok(
        await api.POST("/api/changesets/{changeset_id}/decision", {
          params: { path: { changeset_id: id } },
          body: { decision, note: note.trim() === "" ? null : note.trim() },
        }),
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["review"] }),
  });
  const files = changeset.files ?? [];
  const { from, to } = folders(files);
  const copy = files.some((f) => f.mode === "copy");
  const stamp = changeset.stamp;

  return (
    <Stack gap="sm" data-testid="file-changeset">
      <Group gap="xs">
        <Badge variant="light">{copy ? "copy" : "move"}</Badge>
        <Text size="sm">
          {String(files.length)} file{files.length === 1 ? "" : "s"}
        </Text>
      </Group>
      <Table withRowBorders={false} fz="sm" data-testid="file-folders">
        <Table.Tbody>
          <Table.Tr>
            <Table.Td w={90} c="dimmed">
              Old folder
            </Table.Td>
            <Table.Td ff="monospace" data-testid="old-folder">
              {from}
            </Table.Td>
          </Table.Tr>
          <Table.Tr>
            <Table.Td c="dimmed">New folder</Table.Td>
            <Table.Td ff="monospace" data-testid="new-folder">
              {to}
            </Table.Td>
          </Table.Tr>
        </Table.Tbody>
      </Table>
      {stamp && (
        <Text size="sm" data-testid="file-stamp">
          Sets{" "}
          <Text span ff="monospace">
            {stamp.field}
          </Text>{" "}
          to the day it {copy ? "is copied" : "moves"}.
        </Text>
      )}
      <Table data-testid="file-effects" fz="sm">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Kind</Table.Th>
            <Table.Th>Old name</Table.Th>
            <Table.Th>New name</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {files.map((f) => (
            <Table.Tr key={`${f.src}\u0000${f.dst}`} data-testid="file-effect" data-kind={f.kind}>
              <Table.Td c="dimmed">{KIND_SAYS[f.kind]}</Table.Td>
              <Table.Td ff="monospace" data-testid="old-name">
                {relative(f.src, from)}
              </Table.Td>
              <Table.Td ff="monospace" data-testid="new-name">
                {relative(f.dst, to)}
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <Text size="xs" c="dimmed">
        Accepted or rejected as a whole album, never file by file.
      </Text>
      {decide.error && <Alert color="red">{decide.error.message}</Alert>}
      {decide.isSuccess ? (
        <Text size="sm" c="dimmed" data-testid="decision-sent">
          {decide.variables === "accept" ? (copy ? "Copying…" : "Moving…") : "Rejecting…"}
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
              disabled={decide.isPending}
              loading={decide.isPending && decide.variables === "accept"}
              onClick={() => {
                decide.mutate("accept");
              }}
            >
              {copy ? "Copy album" : "Move album"}
            </Button>
            <Button
              size="xs"
              color="red"
              variant="light"
              disabled={decide.isPending}
              loading={decide.isPending && decide.variables === "reject"}
              onClick={() => {
                decide.mutate("reject");
              }}
            >
              Reject
            </Button>
          </Group>
        </Group>
      )}
    </Stack>
  );
}
