import {
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Checkbox,
  Group,
  Modal,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api, ok } from "../api/client";
import { ImportDecisionView } from "../components/ImportDecisionView";
import type { GateAlbum, QueueItem, ReviewGate } from "../api/types";
import { useProfile, withProfile } from "../hooks/useProfile";
import { useReviewQueue } from "../hooks/useReviewQueue";
import { time } from "../lib/format";
import {
  count,
  currentOf,
  fieldRows,
  formatValue,
  groupByRun,
  reviewLink,
  sameValue,
  targetKey,
  type FieldRow,
  type RunGroup,
} from "../lib/review";

/** The review queue: ChangeSets, review gates and import decisions that wait for a decision, grouped by
 * run (F-REVIEW-QUEUE). Updates come from ``/api/events`` via the app shell. */
export function ReviewPage() {
  const { profile } = useProfile();
  const [params] = useSearchParams();
  const focus = params.get("item");
  const runFilter = params.get("run");
  const queue = useReviewQueue(profile);
  const items = (queue.data ?? []).filter((i) => runFilter === null || i.run_id === runFilter);
  const groups = groupByRun(items);

  // Deep link (REVIEW-QUEUE-16): scroll to the item once it's on the page.
  const focusShown = focus !== null && items.some((i) => i.id === focus);
  useEffect(() => {
    if (!focusShown) return;
    const el = [...document.querySelectorAll<HTMLElement>("[data-item]")].find(
      (e) => e.dataset.item === focus,
    );
    // jsdom has no scrollIntoView.
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition
    el?.scrollIntoView?.({ block: "center" });
  }, [focus, focusShown]);

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>Review</Title>
        {runFilter !== null && (
          <Anchor component={Link} to={withProfile("/review", profile)}>
            Show every run
          </Anchor>
        )}
      </Group>
      {queue.error && <Alert color="red">{queue.error.message}</Alert>}
      {queue.data !== undefined && items.length === 0 && (
        <Text c="dimmed" data-testid="queue-empty">
          Nothing waits for review.
        </Text>
      )}
      {focus !== null && queue.data !== undefined && !focusShown && (
        <Alert color="gray" data-testid="focus-gone">
          That item no longer waits for review.
        </Alert>
      )}
      {groups.map((g) => (
        <RunCard key={g.runId} group={g} focus={focus} />
      ))}
    </Stack>
  );
}

function RunCard({ group, focus }: { group: RunGroup; focus: string | null }) {
  const { profile } = useProfile();
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState<"accept" | "reject" | null>(null);
  const proposed = group.items.filter((i) => i.kind === "changeset" && i.state === "proposed");
  const bulk = useMutation({
    mutationFn: async (decision: "accept" | "reject") =>
      ok(
        await api.POST("/api/runs/{run_id}/decisions", {
          params: { path: { run_id: group.runId } },
          body: { decision },
        }),
      ),
    onSettled: () => {
      setConfirm(null);
      void queryClient.invalidateQueries({ queryKey: ["review"] });
    },
  });
  return (
    <Card withBorder data-testid="run-group" data-run={group.runId}>
      <Stack gap="sm">
        <Group justify="space-between">
          <Group gap="xs">
            <Anchor component={Link} to={withProfile(`/runs/${group.runId}`, profile)} fw={600}>
              {group.workflow ?? group.runId}
            </Anchor>
            <Text size="xs" c="dimmed" ff="monospace">
              {group.runId}
            </Text>
          </Group>
          {proposed.length > 0 && (
            <Group gap="xs">
              <Button
                size="xs"
                variant="light"
                onClick={() => {
                  setConfirm("accept");
                }}
              >
                Accept all
              </Button>
              <Button
                size="xs"
                variant="light"
                color="red"
                onClick={() => {
                  setConfirm("reject");
                }}
              >
                Reject all
              </Button>
            </Group>
          )}
        </Group>
        {bulk.error && <Alert color="red">{bulk.error.message}</Alert>}
        {group.items.map((item) =>
          item.kind === "gate" && item.gate ? (
            <GateCard key={item.id} item={item} gate={item.gate} focused={focus === item.id} />
          ) : item.kind === "import" && item.import_decision ? (
            <ItemCard
              key={item.id}
              item={item}
              focused={focus === item.id}
              title={
                <Group gap="xs">
                  <Text fw={500}>
                    {item.import_decision.kind === "duplicate"
                      ? "Duplicate album"
                      : "Import decision"}
                  </Text>
                  <Text size="sm" c="dimmed">
                    step {item.step_id}
                  </Text>
                </Group>
              }
            >
              <ImportDecisionView decision={item.import_decision} />
            </ItemCard>
          ) : (
            <ChangeSetCard key={item.id} item={item} focused={focus === item.id} />
          ),
        )}
      </Stack>
      <Modal
        opened={confirm !== null}
        onClose={() => {
          setConfirm(null);
        }}
        title={confirm === "accept" ? "Accept all" : "Reject all"}
      >
        <Stack>
          <Text data-testid="bulk-confirm">
            {confirm === "accept" ? "Accept" : "Reject"} {count(proposed.length, "ChangeSet")} of
            this run? Conflicted ChangeSets are left alone.
          </Text>
          <Group justify="flex-end">
            <Button
              variant="default"
              onClick={() => {
                setConfirm(null);
              }}
            >
              Cancel
            </Button>
            <Button
              color={confirm === "reject" ? "red" : undefined}
              loading={bulk.isPending}
              onClick={() => {
                if (confirm !== null) bulk.mutate(confirm);
              }}
            >
              {confirm === "accept" ? "Accept" : "Reject"} {String(proposed.length)}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Card>
  );
}

function ItemCard({
  item,
  focused,
  title,
  children,
}: {
  item: QueueItem;
  focused: boolean;
  title: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <Card
      withBorder
      padding="sm"
      data-testid="queue-item"
      data-item={item.id}
      data-kind={item.kind}
      data-focused={focused ? "true" : undefined}
      style={focused ? { borderColor: "var(--mantine-color-blue-filled)", borderWidth: 2 } : {}}
    >
      <Stack gap="xs">
        <Group justify="space-between">
          {title}
          <Text size="xs" c="dimmed">
            {time(item.created_at)}
          </Text>
        </Group>
        {children}
      </Stack>
    </Card>
  );
}

// -- ChangeSets (REVIEW-QUEUE-12) ----------------------------------------------------------

function ChangeSetCard({ item, focused }: { item: QueueItem; focused: boolean }) {
  const cs = item.changeset;
  const queryClient = useQueryClient();
  const rows = cs ? fieldRows(cs) : [];
  const [checked, setChecked] = useState<Set<number>>(() => new Set(rows.map((r) => r.index)));
  const [note, setNote] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  const current = useQuery({
    queryKey: ["changeset-current", item.id],
    retry: false,
    queryFn: async () =>
      ok(
        await api.GET("/api/changesets/{changeset_id}/current", {
          params: { path: { changeset_id: item.id } },
        }),
      ),
  });
  const decide = useMutation({
    mutationFn: async (body: { decision: "accept" | "reject"; accepted?: number[] }) =>
      ok(
        await api.POST("/api/changesets/{changeset_id}/decision", {
          params: { path: { changeset_id: item.id } },
          body: { ...body, note: note.trim() === "" ? null : note.trim() },
        }),
      ),
    onSuccess: (_d, body) => {
      setSent(body.decision === "accept" ? "Accepting…" : "Rejecting…");
      void queryClient.invalidateQueries({ queryKey: ["review"] });
    },
  });
  const repropose = useMutation({
    mutationFn: async () =>
      ok(
        await api.POST("/api/changesets/{changeset_id}/repropose", {
          params: { path: { changeset_id: item.id } },
        }),
      ),
    onSuccess: () => {
      setSent("Proposing again on the current values…");
      void queryClient.invalidateQueries({ queryKey: ["review"] });
    },
  });
  if (!cs) return null;
  const conflicted = item.state === "conflicted";
  const targets = current.data?.targets;
  const labelOf = (row: FieldRow) =>
    targets?.find((t) => targetKey(t.target) === targetKey(row.target))?.label ??
    `${row.target.scope} ${row.target.id}`;
  const missing = new Set(targets?.filter((t) => t.missing).map((t) => targetKey(t.target)));
  const busy = decide.isPending || repropose.isPending || sent !== null;
  const error = decide.error ?? repropose.error;
  const allChecked = checked.size === rows.length;

  return (
    <ItemCard
      item={item}
      focused={focused}
      title={
        <Group gap="xs">
          <Text fw={500}>ChangeSet</Text>
          {item.step_id && (
            <Text size="sm" c="dimmed">
              from step {item.step_id}
            </Text>
          )}
          <Badge color={conflicted ? "orange" : "blue"} variant="light">
            {item.state}
          </Badge>
        </Group>
      }
    >
      {cs.rationale && <Text size="sm">{cs.rationale}</Text>}
      {conflicted && (
        <Text size="sm" c="orange" data-testid="conflict-note">
          This ChangeSet conflicted: a field changed after the step proposed it. Propose it again on
          the current values, or reject it.
        </Text>
      )}
      {current.error && (
        <Text size="xs" c="dimmed">
          Current values unavailable: {current.error.message}
        </Text>
      )}
      <Table data-testid="field-changes" withRowBorders={false} fz="sm">
        <Table.Thead>
          <Table.Tr>
            {!conflicted && (
              <Table.Th w={32}>
                <Checkbox
                  aria-label="Select all"
                  checked={allChecked}
                  indeterminate={checked.size > 0 && !allChecked}
                  disabled={busy}
                  onChange={() => {
                    setChecked(allChecked ? new Set() : new Set(rows.map((r) => r.index)));
                  }}
                />
              </Table.Th>
            )}
            <Table.Th>Target</Table.Th>
            <Table.Th>Field</Table.Th>
            <Table.Th>{conflicted ? "Expected" : "Step saw"}</Table.Th>
            <Table.Th>{conflicted ? "Current" : "Proposed"}</Table.Th>
            <Table.Th>{conflicted ? "Proposed" : "Now"}</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((row) => {
            const now = currentOf(row, targets);
            const gone = missing.has(targetKey(row.target));
            const changed = now !== undefined && !sameValue(now, row.old);
            const nowCell = (
              <Table.Td
                data-testid="now"
                data-changed={changed ? "true" : undefined}
                c={changed ? "orange" : undefined}
                fw={changed ? 600 : undefined}
              >
                {gone ? "no longer in the library" : now === undefined ? "…" : formatValue(now)}
                {changed && " (changed)"}
              </Table.Td>
            );
            const proposedCell = (
              <Table.Td data-testid="proposed">
                {row.op === "delete" ? "(remove)" : formatValue(row.new)}
              </Table.Td>
            );
            return (
              <Table.Tr key={row.index} data-testid="field-change" data-field={row.field}>
                {!conflicted && (
                  <Table.Td>
                    <Checkbox
                      aria-label={`Accept ${row.field} of ${labelOf(row)}`}
                      checked={checked.has(row.index)}
                      disabled={busy}
                      onChange={() => {
                        setChecked((s) => {
                          const next = new Set(s);
                          if (next.has(row.index)) next.delete(row.index);
                          else next.add(row.index);
                          return next;
                        });
                      }}
                    />
                  </Table.Td>
                )}
                <Table.Td>{labelOf(row)}</Table.Td>
                <Table.Td ff="monospace">{row.field}</Table.Td>
                <Table.Td data-testid="old">{formatValue(row.old)}</Table.Td>
                {conflicted ? nowCell : proposedCell}
                {conflicted ? proposedCell : nowCell}
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
      {error && <Alert color="red">{error.message}</Alert>}
      {sent !== null ? (
        <Text size="sm" c="dimmed" data-testid="decision-sent">
          {sent}
        </Text>
      ) : (
        <Group justify="space-between" align="flex-end">
          {!conflicted ? (
            <TextInput
              size="xs"
              label="Note (optional)"
              value={note}
              onChange={(e) => {
                setNote(e.currentTarget.value);
              }}
              w={260}
            />
          ) : (
            <span />
          )}
          <Group gap="xs">
            {conflicted ? (
              <Button
                size="xs"
                loading={repropose.isPending}
                onClick={() => {
                  repropose.mutate();
                }}
              >
                Propose again on current values
              </Button>
            ) : (
              <Button
                size="xs"
                disabled={checked.size === 0}
                loading={decide.isPending}
                onClick={() => {
                  decide.mutate({
                    decision: "accept",
                    ...(allChecked ? {} : { accepted: [...checked].sort((a, b) => a - b) }),
                  });
                }}
              >
                {allChecked ? "Accept" : "Accept selected"}
              </Button>
            )}
            <Button
              size="xs"
              color="red"
              variant="light"
              loading={decide.isPending}
              onClick={() => {
                decide.mutate({ decision: "reject" });
              }}
            >
              Reject
            </Button>
          </Group>
        </Group>
      )}
    </ItemCard>
  );
}

// -- review gates (REVIEW-QUEUE-13) --------------------------------------------------------

const REJECT_SAYS = {
  stop: "Reject stops the run. Changes it already applied stay.",
  undo: "Reject undoes this run's tag changes, then stops the run.",
} as const;

function GateCard({
  item,
  gate,
  focused,
}: {
  item: QueueItem;
  gate: ReviewGate;
  focused: boolean;
}) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const decide = useMutation({
    mutationFn: async (decision: "continue" | "reject") =>
      ok(
        await api.POST("/api/reviews/{gate_id}/decision", {
          params: { path: { gate_id: gate.id } },
          body: { decision, note: note.trim() === "" ? null : note.trim() },
        }),
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["review"] }),
  });
  const onTimeout = gate.on_timeout ?? "reject";
  return (
    <ItemCard
      item={item}
      focused={focused}
      title={
        <Group gap="xs">
          <Text fw={500}>Review gate</Text>
          <Text size="sm" c="dimmed">
            step {gate.step_id}
          </Text>
        </Group>
      }
    >
      {gate.deadline && (
        <Text size="sm" data-testid="deadline">
          If nobody decides by {time(gate.deadline)}, it will {onTimeout} automatically.
        </Text>
      )}
      {gate.albums.map((album) => (
        <GateAlbumView key={targetKey(album.target)} album={album} />
      ))}
      {decide.error && <Alert color="red">{decide.error.message}</Alert>}
      {decide.isSuccess ? (
        <Text size="sm" c="dimmed" data-testid="decision-sent">
          {decide.data.state === "continued" ? "Continued." : "Rejected."}
        </Text>
      ) : (
        <Stack gap="xs">
          <Text size="xs" c="dimmed" data-testid="reject-says">
            {REJECT_SAYS[gate.on_reject]}
          </Text>
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
                loading={decide.isPending && decide.variables === "continue"}
                disabled={decide.isPending}
                onClick={() => {
                  decide.mutate("continue");
                }}
              >
                Continue
              </Button>
              <Button
                size="xs"
                color="red"
                variant="light"
                loading={decide.isPending && decide.variables === "reject"}
                disabled={decide.isPending}
                onClick={() => {
                  decide.mutate("reject");
                }}
              >
                Reject
              </Button>
            </Group>
          </Group>
        </Stack>
      )}
    </ItemCard>
  );
}

function GateAlbumView({ album }: { album: GateAlbum }) {
  const { profile } = useProfile();
  const shown = Object.entries(album.shown ?? {});
  const changes = album.changes ?? [];
  const conflicted = album.conflicted ?? [];
  return (
    <Stack gap={4} data-testid="gate-album">
      <Group gap="xs">
        <Text fw={500}>{album.label}</Text>
        {album.imported_by_run && (
          <Badge size="sm" variant="light">
            imported by this run
          </Badge>
        )}
      </Group>
      {album.message && <Text size="sm">{album.message}</Text>}
      {shown.length > 0 && (
        <Table fz="sm" withRowBorders={false} data-testid="gate-shown">
          <Table.Tbody>
            {shown.map(([field, value]) => (
              <Table.Tr key={field}>
                <Table.Td ff="monospace" w={160}>
                  {field}
                </Table.Td>
                <Table.Td>{formatValue(value)}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
      {changes.length > 0 ? (
        <Table fz="sm" withRowBorders={false} data-testid="gate-changes">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Changed by this run</Table.Th>
              <Table.Th>Field</Table.Th>
              <Table.Th>Before</Table.Th>
              <Table.Th>After</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {changes.map((c) => (
              <Table.Tr key={`${targetKey(c.target)}/${c.field}`}>
                <Table.Td>{c.target.scope === "album" ? "album" : `item ${c.target.id}`}</Table.Td>
                <Table.Td ff="monospace">{c.field}</Table.Td>
                <Table.Td>{c.existed_before ? formatValue(c.before) : "—"}</Table.Td>
                <Table.Td>{formatValue(c.after)}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      ) : (
        <Text size="xs" c="dimmed">
          No tag changes by this run so far.
        </Text>
      )}
      {conflicted.length > 0 && (
        <Text size="sm" c="orange">
          Conflicted ChangeSets:{" "}
          {conflicted.map((id, n) => (
            <span key={id}>
              {n > 0 && ", "}
              <Anchor component={Link} to={reviewLink(profile, "item", id)}>
                {id}
              </Anchor>
            </span>
          ))}
        </Text>
      )}
    </Stack>
  );
}
