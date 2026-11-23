import { Alert, Anchor, Badge, Button, Group, Modal, Stack, Table, Text } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api, ok } from "../api/client";
import type { InboxEntry, InboxFolder } from "../api/types";
import { useProfile, withProfile } from "../hooks/useProfile";
import { time } from "../lib/format";
import { count } from "../lib/review";
import { tc } from "../theme";

// The inbox of a watch trigger (F-DASHBOARD DASHBOARD-1 .. -3, -19), shown on the dashboard
// and on each workflow's Inbox tab (F-UI-REFRESH UI-REFRESH-26).

const INBOX_COLOR: Record<InboxEntry["state"], string> = {
  settling: "gray",
  waiting: "gray",
  running: "blue",
  waiting_review: "yellow",
  imported: "green",
  failed: "red",
  changed: "orange",
  undone: "violet",
  outside: "dark",
};

/** One watched folder. With `split` (a workflow's Inbox tab, UI-REFRESH-26), inputs not
 * imported yet come first; imported inputs still in the folder (the import left the source
 * files there, as beets' `copy` does) follow below, dimmed and read-only. */
export function InboxFolderView({
  folder,
  split = false,
}: {
  folder: InboxFolder;
  split?: boolean;
}) {
  const pending = split ? folder.entries.filter((e) => e.state !== "imported") : folder.entries;
  const imported = split ? folder.entries.filter((e) => e.state === "imported") : [];
  return (
    <Stack gap={4} data-testid="inbox-folder">
      <Group gap="xs">
        <Text fw={500}>{folder.workflow}</Text>
        <Text size="sm" ff="monospace" c="dimmed">
          {folder.folder}
        </Text>
        <Badge size="sm" variant="light" color={folder.trigger_state === "armed" ? "green" : "red"}>
          {folder.trigger_state}
        </Badge>
        {folder.detail && (
          <Text size="xs" style={{ color: tc("danger") }}>
            {folder.detail}
          </Text>
        )}
        {folder.trigger_state === "armed" && folder.entries.length > 0 && (
          <ImportAll folder={folder} />
        )}
      </Group>
      {folder.entries.length === 0 && (
        <Text size="sm" c="dimmed">
          Empty.
        </Text>
      )}
      {split && folder.entries.length > 0 && (
        <Text size="sm" fw={500}>
          Not imported yet
        </Text>
      )}
      {split && folder.entries.length > 0 && pending.length === 0 && (
        <Text size="sm" c="dimmed" data-testid="inbox-nothing-pending">
          Nothing left to import.
        </Text>
      )}
      {pending.length > 0 && (
        <Table fz="sm" data-testid="inbox-pending">
          <Table.Tbody>
            {pending.map((e) => (
              <InboxRow key={e.path} folder={folder} entry={e} />
            ))}
          </Table.Tbody>
        </Table>
      )}
      {imported.length > 0 && (
        <Stack gap={4} mt="sm" data-testid="inbox-imported" style={{ opacity: 0.55 }}>
          <Text size="sm" fw={500}>
            Already imported
          </Text>
          <Text size="xs" c="dimmed">
            Imported, with their files still in the inbox (the import copied them).
          </Text>
          <Table fz="sm">
            <Table.Tbody>
              {imported.map((e) => (
                <InboxRow key={e.path} folder={folder} entry={e} />
              ))}
            </Table.Tbody>
          </Table>
        </Stack>
      )}
      {folder.omitted > 0 && (
        <Text size="xs" c="dimmed" data-testid="inbox-omitted">
          {count(folder.omitted, "more folder")} not shown.
        </Text>
      )}
    </Stack>
  );
}

/** "Import all now": one forced run per folder, no settle wait (DASHBOARD-19). */
function ImportAll({ folder }: { folder: InboxFolder }) {
  const { profile } = useProfile();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const total = folder.entries.length + folder.omitted;
  const go = useMutation({
    mutationFn: async () =>
      ok(
        await api.POST("/api/profiles/{profile}/inbox/import-all", {
          params: { path: { profile: profile ?? "" } },
          body: { workflow: folder.workflow },
        }),
      ),
    onSuccess: () => {
      setOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["inbox"] });
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });
  return (
    <>
      <Button
        size="compact-xs"
        variant="light"
        ml="auto"
        onClick={() => {
          go.reset();
          setOpen(true);
        }}
      >
        Import all now
      </Button>
      {go.data && (
        <Text size="xs" c="dimmed" data-testid="import-all-result">
          Started {count(go.data.started.length, "run")}
          {go.data.skipped.length > 0 &&
            `; skipped ${go.data.skipped.map((s) => `${s.input} (${s.reason.replace("_", " ")})`).join(", ")}`}
          .
        </Text>
      )}
      <Modal
        opened={open}
        onClose={() => {
          setOpen(false);
        }}
        title="Import all now"
      >
        <Stack gap="sm">
          <Text size="sm">
            Start {folder.workflow} now on {count(total, "folder")} in{" "}
            <Text span ff="monospace">
              {folder.folder}
            </Text>
            , one run each, without waiting for them to settle and even if they were imported
            before. Folders with a run still going are skipped.
          </Text>
          {go.error && <Alert color="red">{go.error.message}</Alert>}
          <Group justify="flex-end">
            <Button
              variant="default"
              onClick={() => {
                setOpen(false);
              }}
            >
              Cancel
            </Button>
            <Button
              loading={go.isPending}
              onClick={() => {
                go.mutate();
              }}
            >
              Import {count(total, "folder")}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
}

function InboxRow({ folder, entry }: { folder: InboxFolder; entry: InboxEntry }) {
  const { profile } = useProfile();
  const queryClient = useQueryClient();
  const again = useMutation({
    mutationFn: async () =>
      ok(
        await api.POST("/api/runs", {
          body: {
            profile: profile ?? "",
            workflow: folder.workflow,
            input: `${folder.folder.replace(/\/+$/, "")}/${entry.path}`,
          },
        }),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["inbox"] });
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });
  const canRetry =
    entry.state === "failed" || entry.state === "changed" || entry.state === "undone";
  return (
    <Table.Tr data-testid="inbox-entry" data-state={entry.state}>
      <Table.Td ff="monospace">{entry.path}</Table.Td>
      <Table.Td>
        <Badge size="sm" variant="light" color={INBOX_COLOR[entry.state]}>
          {entry.state.replace("_", " ")}
        </Badge>
        {entry.state === "settling" && entry.changed_at && (
          <Text span size="xs" c="dimmed" ml="xs">
            changed {time(entry.changed_at)}
          </Text>
        )}
      </Table.Td>
      <Table.Td>
        {entry.run_id && (
          <Anchor
            component={Link}
            to={withProfile(`/runs/${entry.run_id}`, profile)}
            size="xs"
            ff="monospace"
          >
            {entry.run_id}
          </Anchor>
        )}
      </Table.Td>
      <Table.Td>
        {canRetry && !again.isSuccess && (
          <Button
            size="compact-xs"
            variant="light"
            loading={again.isPending}
            onClick={() => {
              again.mutate();
            }}
          >
            Import again
          </Button>
        )}
        {again.isSuccess && (
          <Text size="xs" c="dimmed">
            Started.
          </Text>
        )}
        {again.error && (
          <Text size="xs" style={{ color: tc("danger") }}>
            {again.error.message}
          </Text>
        )}
      </Table.Td>
    </Table.Tr>
  );
}
