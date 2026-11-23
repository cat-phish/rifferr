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
import type { QueueItem, RecentImport, RunSummary } from "../api/types";
import { InboxFolderView } from "../components/Inbox";
import { StatusBadge } from "../components/StatusBadge";
import { useProfile, withProfile } from "../hooks/useProfile";
import { useReviewQueue } from "../hooks/useReviewQueue";
import { time } from "../lib/format";
import { count, reviewLink } from "../lib/review";

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
