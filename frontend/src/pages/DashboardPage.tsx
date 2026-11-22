import {
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Group,
  Modal,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api, ok } from "../api/client";
import type { InboxEntry, InboxFolder, QueueItem, RecentImport, RunSummary } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import { useProfile, withProfile } from "../hooks/useProfile";
import { useReviewQueue } from "../hooks/useReviewQueue";
import { time } from "../lib/format";
import { count, reviewLink } from "../lib/review";
import { tc } from "../theme";

/** Oldest queue items shown on the dashboard (F-DASHBOARD open question 4). */
export const OLDEST_SHOWN = 5;
const IN_PROGRESS = new Set(["queued", "running", "waiting_review"]);

const KIND_LABEL: Record<QueueItem["kind"], string> = {
  changeset: "tag changes",
  gate: "review gates",
  import: "import decisions",
  cleanup: "folder cleanups",
  cover: "cover choices",
  move: "move conflicts",
  prune: "library entries to drop",
};

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

/** The home screen (F-DASHBOARD DASHBOARD-14): what needs review, runs in progress, the
 * inbox and recent imports. The app shell keeps it current from ``/api/events``. */
export function DashboardPage() {
  return (
    <Stack>
      <Title order={2}>Dashboard</Title>
      <NeedsReview />
      <InProgress />
      <Inbox />
      <RecentImports />
    </Stack>
  );
}

function Section({
  title,
  testid,
  children,
}: {
  title: string;
  testid: string;
  children: React.ReactNode;
}) {
  return (
    <Card withBorder data-testid={testid}>
      <Stack gap="sm">
        <Title order={4}>{title}</Title>
        {children}
      </Stack>
    </Card>
  );
}

// -- Needs review (DASHBOARD-5) ---------------------------------------------------------

function itemLabel(item: QueueItem): string {
  if (item.kind === "gate")
    return item.gate?.albums.map((a) => a.label).join(", ") ?? "review gate";
  if (item.kind === "import") return item.import_decision?.path ?? "import decision";
  if (item.kind === "cleanup") return item.cleanup_decision?.path ?? "folder cleanup";
  if (item.kind === "cover") return item.cover_review?.label ?? "cover choice";
  if (item.kind === "move") return item.move_conflict?.label ?? "move conflict";
  const targets = item.changeset?.targets.length ?? 0;
  return `tag changes to ${count(targets, "target")}`;
}

function NeedsReview() {
  const { profile } = useProfile();
  const queue = useReviewQueue(profile);
  const items = queue.data ?? [];
  const kinds = Object.entries(KIND_LABEL)
    .map(([kind, label]) => ({ label, n: items.filter((i) => i.kind === kind).length }))
    .filter((k) => k.n > 0);
  return (
    <Section title="Needs review" testid="needs-review">
      {queue.error && <Alert color="red">{queue.error.message}</Alert>}
      {queue.data !== undefined && items.length === 0 && (
        <Text c="dimmed">Nothing waits for review.</Text>
      )}
      {kinds.length > 0 && (
        <Group gap="xs" data-testid="review-kinds">
          {kinds.map((k) => (
            <Badge key={k.label} variant="light">
              {String(k.n)} {k.label}
            </Badge>
          ))}
          <Anchor component={Link} to={withProfile("/review", profile)} size="sm">
            Open the review queue
          </Anchor>
        </Group>
      )}
      {items.slice(0, OLDEST_SHOWN).map((item) => (
        <Group key={item.id} justify="space-between" data-testid="review-oldest">
          <Anchor component={Link} to={reviewLink(profile, "item", item.id)} size="sm">
            {itemLabel(item)}
          </Anchor>
          <Text size="xs" c="dimmed">
            {item.workflow ?? item.run_id} · {time(item.created_at)}
          </Text>
        </Group>
      ))}
    </Section>
  );
}

// -- Runs in progress (DASHBOARD-4) -----------------------------------------------------

function InProgress() {
  const { profile } = useProfile();
  const runs = useQuery({
    queryKey: ["runs", profile],
    enabled: profile !== null,
    queryFn: async () =>
      ok(await api.GET("/api/runs", { params: { query: { profile, limit: 50 } } })),
  });
  const queue = useReviewQueue(profile);
  const active = (runs.data ?? []).filter((r) => IN_PROGRESS.has(r.state));
  return (
    <Section title="Runs in progress" testid="in-progress">
      {runs.error && <Alert color="red">{runs.error.message}</Alert>}
      {runs.data !== undefined && active.length === 0 && (
        <Text c="dimmed">No run is queued, running or waiting for review.</Text>
      )}
      {active.length > 0 && (
        <Table fz="sm">
          <Table.Tbody>
            {active.map((r) => (
              <RunRow
                key={r.id}
                run={r}
                waitingItems={queue.data?.filter((i) => i.run_id === r.id).length ?? 0}
              />
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Section>
  );
}

function RunRow({ run, waitingItems }: { run: RunSummary; waitingItems: number }) {
  const { profile } = useProfile();
  const lanes = run.lane_counts?.waiting ?? 0;
  return (
    <Table.Tr data-testid="active-run" data-run={run.id}>
      <Table.Td>
        <Anchor component={Link} to={withProfile(`/runs/${run.id}`, profile)}>
          {run.workflow ?? run.id}
        </Anchor>
      </Table.Td>
      <Table.Td>
        <StatusBadge status={run.state} />
      </Table.Td>
      <Table.Td>
        <Badge variant="light" color="gray">
          {run.trigger}
        </Badge>
      </Table.Td>
      <Table.Td ff="monospace" title={run.input ?? undefined}>
        {run.input ?? ""}
      </Table.Td>
      <Table.Td>{time(run.started_at ?? run.created_at)}</Table.Td>
      <Table.Td>
        {run.state === "waiting_review" && (
          <Anchor component={Link} to={reviewLink(profile, "run", run.id)} size="sm">
            {count(lanes, "album")} waiting, {count(waitingItems, "item")} to review
          </Anchor>
        )}
      </Table.Td>
    </Table.Tr>
  );
}

// -- Inbox (DASHBOARD-1 .. -3) ----------------------------------------------------------

function Inbox() {
  const { profile } = useProfile();
  const inbox = useQuery({
    queryKey: ["inbox", profile],
    enabled: profile !== null,
    queryFn: async () =>
      ok(
        await api.GET("/api/profiles/{profile}/inbox", {
          params: { path: { profile: profile ?? "" } },
        }),
      ),
  });
  return (
    <Section title="Inbox" testid="inbox">
      {inbox.error && <Alert color="red">{inbox.error.message}</Alert>}
      {inbox.data !== undefined && inbox.data.length === 0 && (
        <Text c="dimmed">No workflow watches a folder in this profile.</Text>
      )}
      {inbox.data?.map((folder) => (
        <InboxFolderView key={`${folder.workflow}/${folder.folder}`} folder={folder} />
      ))}
    </Section>
  );
}

function InboxFolderView({ folder }: { folder: InboxFolder }) {
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
      {folder.entries.length > 0 && (
        <Table fz="sm">
          <Table.Tbody>
            {folder.entries.map((e) => (
              <InboxRow key={e.path} folder={folder} entry={e} />
            ))}
          </Table.Tbody>
        </Table>
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

// -- Recent imports (DASHBOARD-6, -7, -10) ----------------------------------------------

function RecentImports() {
  const { profile } = useProfile();
  const imports = useQuery({
    queryKey: ["imports", profile],
    enabled: profile !== null,
    retry: false,
    queryFn: async () =>
      ok(
        await api.GET("/api/profiles/{profile}/imports", {
          params: { path: { profile: profile ?? "" } },
        }),
      ),
  });
  return (
    <Section title="Recent imports" testid="recent-imports">
      {imports.error && <Alert color="red">{imports.error.message}</Alert>}
      {imports.data !== undefined && imports.data.length === 0 && (
        <Text c="dimmed">No recent run imported albums that are still in the library.</Text>
      )}
      {imports.data?.map((i) => (
        <ImportView key={i.run_id} imported={i} />
      ))}
    </Section>
  );
}

/** What undo does to one album's files (DASHBOARD-10, -18). */
export function filesSentence(album: RecentImport["albums"][number]): string {
  if (album.deletes_files)
    return "its files are deleted from the library folder (the originals are still in the inbox)";
  return album.files_at ? `its files stay in ${album.files_at}` : "its files stay where they are";
}

function ImportView({ imported }: { imported: RecentImport }) {
  const { profile } = useProfile();
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const undoable = imported.albums.filter((a) => a.undoable);
  const undo = useMutation({
    mutationFn: async () =>
      ok(
        await api.POST("/api/runs/{run_id}/import-undo", {
          params: { path: { run_id: imported.run_id } },
          body: { albums: undoable.map((a) => a.id) },
        }),
      ),
    onSettled: () => {
      setConfirm(false);
      void queryClient.invalidateQueries({ queryKey: ["imports"] });
    },
  });
  return (
    <Stack gap={4} data-testid="recent-import" data-run={imported.run_id}>
      <Group justify="space-between">
        <Group gap="xs">
          <Anchor component={Link} to={withProfile(`/runs/${imported.run_id}`, profile)} fw={500}>
            {imported.workflow ?? imported.run_id}
          </Anchor>
          <StatusBadge status={imported.state} />
          <Text size="xs" c="dimmed">
            {time(imported.finished_at)}
          </Text>
        </Group>
        {undoable.length > 0 && !undo.isSuccess && (
          <Button
            size="xs"
            variant="light"
            color="red"
            onClick={() => {
              setConfirm(true);
            }}
          >
            Undo import
          </Button>
        )}
        {undo.isSuccess && (
          <Text size="xs" c="dimmed" data-testid="undo-queued">
            Removing…
          </Text>
        )}
      </Group>
      {undo.error && <Alert color="red">{undo.error.message}</Alert>}
      {imported.albums.map((a) => (
        <Text key={a.id} size="sm">
          {a.label}{" "}
          <Text span size="xs" c="dimmed">
            {count(a.items, "track")}
            {a.reason ? ` · ${a.reason}` : ""}
          </Text>
        </Text>
      ))}
      <Modal
        opened={confirm}
        onClose={() => {
          setConfirm(false);
        }}
        title="Undo import"
      >
        <Stack>
          <Text size="sm">
            Remove {count(undoable.length, "album")} from the library? The run&apos;s tag changes to
            them are undone first.
          </Text>
          {undoable.map((a) => (
            <Text key={a.id} size="sm" data-testid="undo-album">
              {a.label}: {filesSentence(a)}.
            </Text>
          ))}
          <Group justify="flex-end">
            <Button
              variant="default"
              onClick={() => {
                setConfirm(false);
              }}
            >
              Cancel
            </Button>
            <Button
              color="red"
              loading={undo.isPending}
              onClick={() => {
                undo.mutate();
              }}
            >
              Remove {String(undoable.length)}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
