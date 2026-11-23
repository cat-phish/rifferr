import {
  Alert,
  Anchor,
  Badge,
  Box,
  Button,
  Card,
  Checkbox,
  Group,
  Modal,
  Stack,
  Table,
  Tabs,
  Text,
  Title,
} from "@mantine/core";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { Link, Navigate, useSearchParams } from "react-router";
import { api, ok } from "../api/client";
import { CoverPicker } from "../components/CoverPicker";
import { CleanupDecisionView } from "../components/CleanupDecisionView";
import { FileChangeSetView } from "../components/FileChangeSetView";
import { MoveConflictView } from "../components/MoveConflictView";
import { PruneDecisionView } from "../components/PruneDecisionView";
import { ActionsSlotContext, HeaderActions } from "../components/HeaderActions";
import { DecidedContext, ItemIdContext, jobOf } from "../components/Decided";
import { Diff } from "../components/Diff";
import { useFold } from "../components/Fold";
import { NoteField } from "../components/NoteField";
import { TokenBadge } from "../components/MatchBadge";
import { tc } from "../theme";
import { ImportDecisionView } from "../components/ImportDecisionView";
import type { GateAlbum, QueueItem, ReviewGate } from "../api/types";
import { useProfile, withProfile } from "../hooks/useProfile";
import { usePipeline, useReviewQueue, useWorkflowSteps } from "../hooks/useReviewQueue";
import { BULK_ACTIONS, bulkKind, decideAll, type BulkFailure, type BulkKind } from "../lib/bulk";
import { useAppEventListener } from "../lib/appEvents";
import { duration, time } from "../lib/format";
import { StatusBadge } from "../components/StatusBadge";
import {
  count,
  currentOf,
  FINISHING_STAGE,
  fieldRows,
  formatValue,
  groupByStep,
  laneOf,
  pipelineByStep,
  type PipelineEntry,
  reviewWorkflows,
  stageTabs,
  type ReviewWorkflow,
  reviewLink,
  sameValue,
  targetKey,
  type FieldRow,
  type StepSection,
  workflowLink,
} from "../lib/review";

interface Selection {
  selected: Map<string, QueueItem>;
  kind: BulkKind | null;
  toggle: (item: QueueItem) => void;
}

/** Items picked to decide together (F-DASHBOARD DASHBOARD-13). */
const SelectionContext = createContext<Selection>({
  selected: new Map(),
  kind: null,
  toggle: () => undefined,
});

interface DecidedEntry {
  /** The item as it was when decided; a change brings it back. */
  sig: string;
  jobId: string | null;
  /** When its job finished (`job.finished`); a refetch after that brings it back. */
  finishedAt: number | null;
}

/** The review queue: ChangeSets, review gates, import and cleanup decisions that wait for a decision, grouped by
 * run (F-REVIEW-QUEUE). Updates come from ``/api/events`` via the app shell. Several items
 * of one kind can be decided together (F-DASHBOARD DASHBOARD-13). */
export function ReviewPage({
  workflow: fixed,
  hub = false,
}: { workflow?: string; hub?: boolean } = {}) {
  const { profile } = useProfile();
  // Inside a workflow's page (`workflow`), on the Workflows hub (`hub`, UI-REFRESH-23), or
  // the old `/review` link, which goes on to the workflow's page.
  const embedded = fixed !== undefined;
  const [params, setParams] = useSearchParams();
  const focus = params.get("item");
  const runFilter = params.get("run");
  const queue = useReviewQueue(profile);
  const items = (queue.data ?? []).filter((i) => runFilter === null || i.run_id === runFilter);
  // One run per workflow gives that workflow's step order for the sections (same query and
  // cache as the run page; lane and step events refresh it).
  const firstRuns = [...new Map(items.map((i) => [i.workflow ?? "", i.run_id])).entries()];
  const runs = useQueries({
    queries: firstRuns.map(([, runId]) => ({
      queryKey: ["run", runId],
      retry: false,
      queryFn: async () =>
        ok(await api.GET("/api/runs/{run_id}", { params: { path: { run_id: runId } } })),
    })),
  });
  const stepOrders = new Map<string, string[]>();
  firstRuns.forEach(([workflow], i) => {
    const data = runs[i]?.data;
    if (data)
      stepOrders.set(
        workflow,
        data.steps.map((st) => st.id),
      );
  });
  const queryClient = useQueryClient();
  const [picked, setPicked] = useState<Map<string, QueueItem>>(new Map());
  const [failures, setFailures] = useState<BulkFailure[]>([]);
  // Items decided elsewhere drop out of the selection.
  const selected = new Map([...picked].filter(([id]) => items.some((i) => i.id === id)));
  const first = selected.values().next();
  const kind = first.done ? null : bulkKind(first.value);
  const selection: Selection = {
    selected,
    kind,
    toggle: (item) => {
      setPicked((old) => {
        const next = new Map(old);
        if (next.has(item.id)) next.delete(item.id);
        else next.set(item.id, item);
        return next;
      });
    },
  };
  // Items decided here leave the waiting list at once (UI-REFRESH-20). One comes back if
  // it changes (an error, a conflict) or its job finishes while it still waits.
  const [decidedMap, setDecided] = useState<Map<string, DecidedEntry>>(new Map());
  const markDecided = useCallback(
    (itemId: string, jobId?: string | null) => {
      const item = (queue.data ?? []).find((i) => i.id === itemId);
      if (item === undefined) return;
      setDecided((old) =>
        new Map(old).set(itemId, {
          sig: JSON.stringify(item),
          jobId: jobId ?? null,
          finishedAt: null,
        }),
      );
    },
    [queue.data],
  );
  useAppEventListener((event) => {
    // Only a job that failed brings its item back: after a successful decision job the
    // run still has to carry the decision out (a library move applies when the run
    // resumes), and the item leaves once it has (UI-REFRESH-20).
    if (event.type !== "job.finished" || event.data.state === "succeeded") return;
    const job = event.data.job_id;
    setDecided((old) => {
      let changed = false;
      const next = new Map(old);
      for (const [id, e] of old) {
        if (e.jobId !== null && e.jobId === job && e.finishedAt === null) {
          next.set(id, { ...e, finishedAt: Date.now() });
          changed = true;
        }
      }
      return changed ? next : old;
    });
  });
  const decided = new Set(
    [...decidedMap]
      .filter(([id, e]) => {
        const item = items.find((i) => i.id === id);
        if (item === undefined || JSON.stringify(item) !== e.sig) return false;
        return e.finishedAt === null || queue.dataUpdatedAt <= e.finishedAt;
      })
      .map(([id]) => id),
  );
  // One workflow at a time (UI-REFRESH-22): a deep-linked item's, else the URL's, else the
  // first with items. Its stage tabs are permanent: one per step that can wait, with its
  // count (UI-REFRESH-13). A deep-linked item picks its tab; otherwise the URL's `stage`,
  // else the first tab with items. Both go into the URL, so nothing switches on its own.
  const details = useWorkflowSteps(profile);
  for (const d of details)
    if ((d.steps ?? []).length > 0)
      stepOrders.set(
        d.name,
        (d.steps ?? []).map((st) => st.id),
      );
  const workflows = reviewWorkflows(details, items, hub);
  // The hub is the overview: every workflow with its stages (UI-REFRESH-22, -23).
  const overview = hub;
  const focusItem = items.find((i) => i.id === focus);
  const urlWorkflow = embedded ? fixed : params.get("workflow");
  const workflowName = embedded
    ? fixed
    : ((focusItem && (focusItem.workflow ?? "")) ??
      urlWorkflow ??
      (workflows.find((w) => w.count > 0) ?? workflows[0])?.name);
  const workflow = workflows.find((w) => w.name === workflowName) ?? {
    name: workflowName ?? "",
    steps: [],
    waits: [],
    count: 0,
  };
  const sections = groupByStep(
    items.filter((i) => (i.workflow ?? "") === workflowName),
    stepOrders,
    decided,
  );
  // The Pipeline tab (UI-REFRESH-24): a stage tab per step, with the albums at each step
  // that don't wait on review; waiting items stay first.
  const pipeline = usePipeline(profile, embedded ? workflowName : undefined);
  const shownIds = new Set(
    sections.flatMap((sec) => sec.items.map((i) => i.id)).filter((id) => !decided.has(id)),
  );
  const atStep = pipelineByStep(
    workflow.steps,
    pipeline.data?.lanes ?? [],
    pipeline.data?.starting ?? [],
    shownIds,
  );
  const tabs = stageTabs(workflow, sections, embedded);
  if (embedded && (atStep.get(FINISHING_STAGE)?.length ?? 0) > 0)
    tabs.push({ value: FINISHING_STAGE, label: "Finishing", section: undefined });
  const albumsAt = (value: string) => atStep.get(value) ?? [];
  const focusTab = tabs.find((t) => t.section?.items.some((i) => i.id === focus));
  const urlStage = params.get("stage");
  const stage =
    focusTab?.value ??
    urlStage ??
    (tabs.find((t) => t.section) ?? tabs.find((t) => albumsAt(t.value).length > 0) ?? tabs[0])
      ?.value;
  const waitingTabs = tabs.filter((t) => (t.section?.items.length ?? 0) > 0);
  const shown = tabs.find((t) => t.value === stage)?.section;
  useEffect(() => {
    if (!embedded) return;
    const want: [string, string | undefined][] = [["stage", stage]];
    if (want.every(([k, v]) => v === undefined || params.get(k) === v)) return;
    setParams(
      (old) => {
        const next = new URLSearchParams(old);
        for (const [k, v] of want) if (v !== undefined) next.set(k, v);
        return next;
      },
      { replace: true },
    );
  }, [embedded, stage, params, setParams]);
  const pickStage = (value: string | null) => {
    if (value === null) return;
    setPicked(new Map());
    setParams((old) => {
      const next = new URLSearchParams(old);
      next.set("stage", value);
      next.delete("item");
      return next;
    });
  };
  const bulk = useMutation({
    mutationFn: async (action: string) => decideAll([...selected.values()], action),
    onSuccess: (failed) => {
      setFailures(failed);
      setPicked(new Map());
    },
    onSettled: () => void queryClient.invalidateQueries({ queryKey: ["review"] }),
  });

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

  if (!embedded && !overview) {
    // The old `/review` page (notifications, run pages): on to the item's or run's
    // workflow once the queue is known; with nothing to pick, to the hub (UI-REFRESH-23).
    if (queue.data === undefined) return null;
    const pickedName =
      focus !== null ||
      runFilter !== null ||
      params.get("workflow") !== null ||
      workflows.length === 1
        ? workflowName
        : undefined;
    if (pickedName === undefined)
      return <Navigate replace to={withProfile("/workflows", profile)} />;
    return (
      <Navigate
        replace
        to={workflowLink(profile, pickedName, {
          stage: params.get("stage"),
          item: focus,
          run: runFilter,
        })}
      />
    );
  }
  if (overview)
    return (
      <ReviewOverview
        workflows={workflows}
        items={items}
        stepOrders={stepOrders}
        loaded={queue.data !== undefined}
        error={queue.error?.message ?? null}
      />
    );

  return (
    <Stack>
      {runFilter !== null && (
        <Group justify="flex-end">
          <Anchor component={Link} to={workflowLink(profile, workflowName ?? "")}>
            Show every run
          </Anchor>
        </Group>
      )}
      {queue.error && <Alert color="red">{queue.error.message}</Alert>}
      {kind !== null && (
        <Card withBorder padding="xs" data-testid="bulk-bar">
          <Group justify="space-between">
            <Text size="sm">{count(selected.size, "item")} selected</Text>
            <Group gap="xs">
              {BULK_ACTIONS[kind].map((a) => (
                <Button
                  key={a.value}
                  size="xs"
                  color={a.danger ? "red" : undefined}
                  variant={a.danger ? "light" : "filled"}
                  loading={bulk.isPending && bulk.variables === a.value}
                  disabled={bulk.isPending}
                  onClick={() => {
                    bulk.mutate(a.value);
                  }}
                >
                  {a.label} selected
                </Button>
              ))}
              <Button
                size="xs"
                variant="default"
                disabled={bulk.isPending}
                onClick={() => {
                  setPicked(new Map());
                }}
              >
                Clear
              </Button>
            </Group>
          </Group>
        </Card>
      )}
      {failures.length > 0 && (
        <Alert
          color="red"
          data-testid="bulk-failures"
          withCloseButton
          onClose={() => {
            setFailures([]);
          }}
        >
          {count(failures.length, "item")} could not be decided:
          {failures.map((f) => (
            <Text key={f.id} size="sm">
              {f.id}: {f.message}
            </Text>
          ))}
        </Alert>
      )}
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
      {embedded && waitingTabs.length > 0 && (
        <Card withBorder padding="xs" data-testid="waiting-bar">
          <Group gap="xs">
            <Text size="sm" fw={600}>
              Waiting for review
            </Text>
            {waitingTabs.map((t) => (
              <Button
                key={t.value}
                size="compact-xs"
                variant={t.value === stage ? "filled" : "light"}
                data-testid="waiting-chip"
                onClick={() => {
                  pickStage(t.value);
                }}
              >
                {t.label} · {String(t.section?.items.length ?? 0)}
              </Button>
            ))}
          </Group>
        </Card>
      )}
      <SelectionContext.Provider value={selection}>
        <DecidedContext.Provider value={markDecided}>
          {stage !== undefined && (tabs.length > 0 || items.length > 0) && (
            <Tabs value={stage} onChange={pickStage} keepMounted={false}>
              <Tabs.List data-testid="stage-tabs">
                {tabs.map((t) => (
                  <StageTabButton
                    key={t.value}
                    value={t.value}
                    label={t.label}
                    n={t.section?.items.length ?? 0}
                    albums={embedded ? albumsAt(t.value).length : undefined}
                  />
                ))}
                {!tabs.some((t) => t.value === stage) && (
                  <StageTabButton value={stage} label={stage} n={0} />
                )}
              </Tabs.List>
              <Box pt="sm">
                {shown ? (
                  <StepSectionCard key={shown.key} section={shown} focus={focus} />
                ) : (
                  <Text c="dimmed" data-testid="stage-empty">
                    Nothing waiting at this step.
                  </Text>
                )}
                {embedded && albumsAt(stage).length > 0 && <AtThisStep entries={albumsAt(stage)} />}
              </Box>
            </Tabs>
          )}
        </DecidedContext.Provider>
      </SelectionContext.Provider>
    </Stack>
  );
}

/** A proposed ChangeSet that moves (or copies) an album's files (S-LIBRARY-MOVE). */
function isFileMove(item: QueueItem): boolean {
  return (item.changeset?.files?.length ?? 0) > 0;
}

/** Every workflow that can wait for review, with its stages and counts (UI-REFRESH-22). */
function ReviewOverview({
  workflows,
  items,
  stepOrders,
  loaded,
  error,
}: {
  workflows: readonly ReviewWorkflow[];
  items: readonly QueueItem[];
  stepOrders: ReadonlyMap<string, readonly string[]>;
  loaded: boolean;
  error: string | null;
}) {
  const { profile } = useProfile();
  const link = (name: string, stage?: string) => workflowLink(profile, name, { stage });
  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>Workflows</Title>
        <Button
          component={Link}
          to={withProfile("/workflow-files", profile)}
          variant="light"
          size="xs"
          data-testid="edit-workflows"
        >
          Edit workflows
        </Button>
      </Group>
      {error !== null && <Alert color="red">{error}</Alert>}
      {loaded && workflows.length === 0 && (
        <Text c="dimmed" data-testid="queue-empty">
          Nothing waits for review.
        </Text>
      )}
      {workflows.map((w) => {
        const mine = items.filter((i) => (i.workflow ?? "") === w.name);
        const tabs = stageTabs(w, groupByStep(mine, stepOrders));
        const oldest = mine.map((i) => i.created_at).sort()[0];
        return (
          <Card key={w.name} withBorder padding="sm" data-testid="review-workflow-card">
            <Group justify="space-between" align="baseline">
              <Group gap="xs" align="baseline">
                <Anchor component={Link} to={link(w.name)} fw={600}>
                  {w.name || "Other"}
                </Anchor>
                <Text size="sm" c="dimmed" data-testid="workflow-waiting">
                  {w.count === 0 ? "nothing waiting" : `${count(w.count, "item")} waiting`}
                  {oldest !== undefined && ` · oldest ${time(oldest)}`}
                </Text>
              </Group>
            </Group>
            <Group gap="xs" mt={6} wrap="wrap">
              {tabs.map((t) => {
                const n = t.section?.items.length ?? 0;
                return (
                  <Anchor
                    key={t.value}
                    component={Link}
                    to={link(w.name, t.value)}
                    size="sm"
                    c={n === 0 ? "dimmed" : undefined}
                    data-testid="overview-stage"
                  >
                    {t.label}{" "}
                    <Badge size="sm" variant="light" circle color={n === 0 ? "gray" : undefined}>
                      {String(n)}
                    </Badge>
                  </Anchor>
                );
              })}
            </Group>
          </Card>
        );
      })}
    </Stack>
  );
}

/** One stage tab with its count; dimmed while nothing waits there (UI-REFRESH-13). With
 * `albums`, also the count of albums at that step not waiting on review (UI-REFRESH-24). */
function StageTabButton({
  value,
  label,
  n,
  albums,
}: {
  value: string;
  label: string;
  n: number;
  albums?: number;
}) {
  return (
    <Tabs.Tab
      value={value}
      data-testid="stage-tab"
      c={n === 0 && !albums ? "dimmed" : undefined}
      rightSection={
        <Group gap={4} wrap="nowrap">
          <Badge
            size="sm"
            variant="light"
            circle
            color={n === 0 ? "gray" : undefined}
            data-testid="stage-count"
          >
            {String(n)}
          </Badge>
          {albums !== undefined && albums > 0 && (
            <Text size="xs" c="dimmed" data-testid="stage-albums" title="Albums at this step">
              +{String(albums)}
            </Text>
          )}
        </Group>
      }
    >
      {label}
    </Tabs.Tab>
  );
}

/** The albums at a step that don't wait on review (UI-REFRESH-24); read-only. */
function AtThisStep({ entries }: { entries: readonly PipelineEntry[] }) {
  const { profile } = useProfile();
  return (
    <Stack gap="xs" mt="md" data-testid="at-this-step">
      <Text size="sm" fw={600}>
        At this step
      </Text>
      <Table>
        <Table.Tbody>
          {entries.map((e) => (
            <Table.Tr key={e.key} data-testid="pipeline-album">
              <Table.Td>{e.label}</Table.Td>
              <Table.Td>
                <StatusBadge status={e.state} />
              </Table.Td>
              <Table.Td>
                <Text size="xs" c="dimmed">
                  {duration(e.since)}
                </Text>
              </Table.Td>
              <Table.Td>
                <Anchor component={Link} to={withProfile(`/runs/${e.runId}`, profile)} size="sm">
                  Run
                </Anchor>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}

/** One workflow step's waiting items, oldest first (REVIEW-QUEUE-11, UI-REFRESH-13). */
function StepSectionCard({ section, focus }: { section: StepSection; focus: string | null }) {
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState<"accept" | "reject" | null>(null);
  const proposed = section.items.filter((i) => i.kind === "changeset" && i.state === "proposed");
  // A stage of library moves reads "Move all N" (UI-REFRESH-21).
  const moves = proposed.length > 0 && proposed.every(isFileMove);
  const copies = moves && proposed.some((i) => i.changeset?.files?.some((f) => f.mode === "copy"));
  const acceptVerb = moves ? (copies ? "Copy" : "Move") : "Accept";
  const what = moves ? "album" : "ChangeSet";
  const [failures, setFailures] = useState<BulkFailure[]>([]);
  const bulk = useMutation({
    mutationFn: async (decision: "accept" | "reject") => decideAll(proposed, decision),
    onSuccess: (failed) => {
      setFailures(failed);
    },
    onSettled: () => {
      setConfirm(null);
      void queryClient.invalidateQueries({ queryKey: ["review"] });
    },
  });
  return (
    <Card withBorder data-testid="step-section" data-step={section.stepId ?? ""}>
      <Stack gap="sm">
        <Group justify="space-between">
          <Group gap="xs">
            <Text fw={600} data-testid="step-label">
              {section.label}
            </Text>
            <Text size="xs" c="dimmed" data-testid="step-count">
              {count(section.items.length, "item")}
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
                {moves ? `${acceptVerb} all ${String(proposed.length)}` : "Accept all"}
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
        {failures.length > 0 && (
          <Alert color="red" data-testid="section-failures">
            {count(failures.length, "item")} could not be decided.
          </Alert>
        )}
        {section.items.map((item) => (
          <QueueItemCard key={item.id} item={item} focus={focus} />
        ))}
      </Stack>
      <Modal
        opened={confirm !== null}
        onClose={() => {
          setConfirm(null);
        }}
        title={confirm === "accept" ? `${acceptVerb} all` : "Reject all"}
      >
        <Stack>
          <Text data-testid="bulk-confirm">
            {confirm === "accept" ? acceptVerb : "Reject"} {count(proposed.length, what)} of{" "}
            {section.label}? {moves ? "Albums with a conflict" : "Conflicted ChangeSets"} are left
            alone.
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
              {confirm === "accept" ? acceptVerb : "Reject"} {String(proposed.length)}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Card>
  );
}

/** The item's album (its lane label) and a link to its run (REVIEW-QUEUE-11). */
function AlbumLabel({ item }: { item: QueueItem }) {
  const { profile } = useProfile();
  const run = useQuery({
    queryKey: ["run", item.run_id],
    retry: false,
    queryFn: async () =>
      ok(await api.GET("/api/runs/{run_id}", { params: { path: { run_id: item.run_id } } })),
  });
  const lane = run.data ? laneOf(item, run.data.lanes ?? []) : null;
  return (
    <Group gap={6} wrap="nowrap">
      {lane !== null && (
        <Text size="sm" fw={500} data-testid="item-album">
          {lane.label}
        </Text>
      )}
      <Anchor
        component={Link}
        to={withProfile(`/runs/${item.run_id}`, profile)}
        size="xs"
        c="dimmed"
        data-testid="item-run"
      >
        run
      </Anchor>
    </Group>
  );
}

/** The card for one queue item, by kind. */
function QueueItemCard({ item, focus }: { item: QueueItem; focus: string | null }) {
  return item.kind === "gate" && item.gate ? (
    <GateCard key={item.id} item={item} gate={item.gate} focused={focus === item.id} />
  ) : item.kind === "import" && item.import_decision ? (
    <ItemCard
      key={item.id}
      item={item}
      focused={focus === item.id}
      title={
        <Group gap="xs">
          <Text fw={500}>
            {item.import_decision.kind === "duplicate" ? "Duplicate album" : "Import decision"}
          </Text>
          <Text size="sm" c="dimmed">
            step {item.step_id}
          </Text>
        </Group>
      }
    >
      <ImportDecisionView decision={item.import_decision} />
    </ItemCard>
  ) : item.kind === "prune" && item.prune_decision ? (
    <ItemCard
      key={item.id}
      item={item}
      focused={focus === item.id}
      title={
        <Group gap="xs">
          <Text fw={500}>Library entries without files</Text>
          <Text size="sm" c="dimmed">
            step {item.step_id}
          </Text>
        </Group>
      }
    >
      <PruneDecisionView decision={item.prune_decision} />
    </ItemCard>
  ) : item.kind === "cleanup" && item.cleanup_decision ? (
    <ItemCard
      key={item.id}
      item={item}
      focused={focus === item.id}
      title={
        <Group gap="xs">
          <Text fw={500}>Folder cleanup</Text>
          <Text size="sm" c="dimmed">
            step {item.step_id}
          </Text>
        </Group>
      }
    >
      <CleanupDecisionView decision={item.cleanup_decision} />
    </ItemCard>
  ) : item.kind === "move" && item.move_conflict ? (
    <ItemCard
      key={item.id}
      item={item}
      focused={focus === item.id}
      title={
        <Group gap="xs">
          <Text fw={500}>Move conflict</Text>
          <Text size="sm" c="dimmed">
            step {item.step_id}
          </Text>
        </Group>
      }
    >
      <MoveConflictView conflict={item.move_conflict} />
    </ItemCard>
  ) : item.kind === "cover" && item.cover_review ? (
    <ItemCard
      key={item.id}
      item={item}
      focused={focus === item.id}
      title={
        <Group gap="xs">
          <Text fw={500}>Cover choice</Text>
          <Text size="sm" c="dimmed">
            step {item.step_id}
          </Text>
        </Group>
      }
    >
      <CoverPicker
        profile={item.cover_review.profile}
        mode={{ kind: "review", review: item.cover_review }}
      />
    </ItemCard>
  ) : item.kind === "changeset" &&
    item.changeset &&
    (item.changeset.files?.length ?? 0) > 0 &&
    item.state === "proposed" ? (
    <ItemCard
      key={item.id}
      item={item}
      focused={focus === item.id}
      title={
        <Group gap="xs">
          <Text fw={500}>Library move</Text>
          <Text size="sm" c="dimmed">
            from step {item.step_id}
          </Text>
        </Group>
      }
    >
      <FileChangeSetView id={item.id} changeset={item.changeset} />
    </ItemCard>
  ) : (
    <ChangeSetCard key={item.id} item={item} focused={focus === item.id} />
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
  const selection = useContext(SelectionContext);
  const kind = bulkKind(item);
  const [slot, setSlot] = useState<HTMLDivElement | null>(null);
  return (
    <Card
      withBorder
      padding="xs"
      data-testid="queue-item"
      data-item={item.id}
      data-kind={item.kind}
      data-focused={focused ? "true" : undefined}
      style={focused ? { borderColor: "var(--mantine-color-blue-filled)", borderWidth: 2 } : {}}
    >
      <Stack gap={6}>
        {/* One header row: select, kind, age, then the item's actions on the right (UI-REFRESH-10). */}
        <Group justify="space-between" wrap="wrap" gap="xs">
          <Group gap="xs" wrap="nowrap">
            {kind !== null && (
              <Checkbox
                aria-label="Select"
                data-testid="select-item"
                checked={selection.selected.has(item.id)}
                disabled={selection.kind !== null && selection.kind !== kind}
                onChange={() => {
                  selection.toggle(item);
                }}
              />
            )}
            {title}
            <AlbumLabel item={item} />
            <Text size="xs" c="dimmed">
              {time(item.created_at)}
            </Text>
          </Group>
          <div ref={setSlot} data-testid="item-actions" style={{ marginLeft: "auto" }} />
        </Group>
        <ItemIdContext.Provider value={item.id}>
          <ActionsSlotContext.Provider value={slot}>{children}</ActionsSlotContext.Provider>
        </ItemIdContext.Provider>
      </Stack>
    </Card>
  );
}

// -- ChangeSets (REVIEW-QUEUE-12) ----------------------------------------------------------

function ChangeSetCard({ item, focused }: { item: QueueItem; focused: boolean }) {
  const cs = item.changeset;
  const queryClient = useQueryClient();
  const rows = cs ? fieldRows(cs) : [];
  const fold = useFold(rows);
  const markItem = useContext(DecidedContext);
  const markDecided = (jobId?: string | null) => {
    markItem(item.id, jobId);
  };
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
    onSuccess: (d, body) => {
      markDecided(jobOf(d));
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
    onSuccess: (d) => {
      markDecided(jobOf(d));
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
          <TokenBadge token={conflicted ? "warning" : null}>{item.state}</TokenBadge>
        </Group>
      }
    >
      {cs.rationale && <Text size="sm">{cs.rationale}</Text>}
      {conflicted && (
        <Text size="sm" style={{ color: tc("warning") }} data-testid="conflict-note">
          ⚠ This ChangeSet conflicted: a field changed after the step proposed it. Propose it again
          on the current values, or reject it.
        </Text>
      )}
      {current.error && (
        <Text size="xs" c="dimmed">
          Current values unavailable: {current.error.message}
        </Text>
      )}
      <Table data-testid="field-changes" withRowBorders={false} fz="sm" verticalSpacing={2}>
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
          {fold.shown.map((row) => {
            const now = currentOf(row, targets);
            const gone = missing.has(targetKey(row.target));
            const changed = now !== undefined && !sameValue(now, row.old);
            const nowCell = (
              <Table.Td
                data-testid="now"
                data-changed={changed ? "true" : undefined}
                style={changed ? { color: tc("warning"), fontWeight: 600 } : undefined}
                c={changed ? undefined : "dimmed"}
              >
                {gone ? "no longer in the library" : now === undefined ? "…" : formatValue(now)}
                {changed && " (changed)"}
              </Table.Td>
            );
            const proposedCell = (
              <Table.Td
                data-testid="proposed"
                data-new={row.op === "delete" ? "" : formatValue(row.new)}
              >
                {row.op === "delete" ? (
                  <Diff old={formatValue(row.old)} new="" empty="(remove)" />
                ) : (
                  <Diff old={formatValue(row.old)} new={formatValue(row.new)} />
                )}
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
                <Table.Td data-testid="old" c="dimmed">
                  {formatValue(row.old)}
                </Table.Td>
                {conflicted ? nowCell : proposedCell}
                {conflicted ? proposedCell : nowCell}
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
      {fold.control}
      {error && <Alert color="red">{error.message}</Alert>}
      {sent !== null ? (
        <HeaderActions>
          <Text size="sm" c="dimmed" data-testid="decision-sent">
            {sent}
          </Text>
        </HeaderActions>
      ) : (
        <>
          {!conflicted && <NoteField value={note} onChange={setNote} />}
          <HeaderActions>
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
          </HeaderActions>
        </>
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
  const markItem = useContext(DecidedContext);
  const markDecided = (jobId?: string | null) => {
    markItem(item.id, jobId);
  };
  const [note, setNote] = useState("");
  const decide = useMutation({
    mutationFn: async (decision: "continue" | "reject") =>
      ok(
        await api.POST("/api/reviews/{gate_id}/decision", {
          params: { path: { gate_id: gate.id } },
          body: { decision, note: note.trim() === "" ? null : note.trim() },
        }),
      ),
    onSuccess: () => {
      markDecided();
      void queryClient.invalidateQueries({ queryKey: ["review"] });
    },
  });
  // One album at a time (F-DASHBOARD DASHBOARD-11, -12); only on gates of several albums.
  const perAlbum = useMutation({
    mutationFn: async (v: { decision: "continue" | "reject"; album: string }) =>
      ok(
        await api.POST("/api/reviews/{gate_id}/decision", {
          params: { path: { gate_id: gate.id } },
          body: {
            decision: v.decision,
            note: note.trim() === "" ? null : note.trim(),
            albums: [v.album],
          },
        }),
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["review"] }),
  });
  const decided = perAlbum.data?.album_decisions ?? gate.album_decisions ?? {};
  const several = gate.albums.length > 1;
  const undecided = gate.albums.filter((a) => !(a.target.id in decided)).length;
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
      {gate.albums.map((album) => {
        const verdict = decided[album.target.id];
        return (
          <Stack key={targetKey(album.target)} gap={4}>
            <GateAlbumView album={album} />
            {several && verdict !== undefined && (
              <TokenBadge
                token={verdict === "continued" ? "success" : "danger"}
                data-testid="album-decided"
              >
                {verdict}
              </TokenBadge>
            )}
            {several && verdict === undefined && !decide.isSuccess && (
              <Group gap="xs" data-testid="album-actions">
                <Button
                  size="compact-xs"
                  variant="light"
                  disabled={perAlbum.isPending || decide.isPending}
                  onClick={() => {
                    perAlbum.mutate({ decision: "continue", album: album.target.id });
                  }}
                >
                  Continue this album
                </Button>
                <Button
                  size="compact-xs"
                  variant="subtle"
                  color="red"
                  disabled={perAlbum.isPending || decide.isPending}
                  onClick={() => {
                    perAlbum.mutate({ decision: "reject", album: album.target.id });
                  }}
                >
                  Reject this album
                </Button>
              </Group>
            )}
          </Stack>
        );
      })}
      {perAlbum.error && <Alert color="red">{perAlbum.error.message}</Alert>}
      {decide.error && <Alert color="red">{decide.error.message}</Alert>}
      {decide.isSuccess ? (
        <HeaderActions>
          <Text size="sm" c="dimmed" data-testid="decision-sent">
            {decide.data.state === "continued" ? "Continued." : "Rejected."}
          </Text>
        </HeaderActions>
      ) : (
        <>
          <Group gap="xs" align="flex-end">
            <NoteField value={note} onChange={setNote} />
            <Text size="xs" c="dimmed" data-testid="reject-says">
              {REJECT_SAYS[gate.on_reject]}
            </Text>
          </Group>
          <HeaderActions>
            <Button
              size="xs"
              loading={decide.isPending && decide.variables === "continue"}
              disabled={decide.isPending}
              onClick={() => {
                decide.mutate("continue");
              }}
            >
              {several && undecided < gate.albums.length ? "Continue the rest" : "Continue"}
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
              {several && undecided < gate.albums.length ? "Reject the rest" : "Reject"}
            </Button>
          </HeaderActions>
        </>
      )}
    </ItemCard>
  );
}

function GateAlbumView({ album }: { album: GateAlbum }) {
  const { profile } = useProfile();
  const shown = Object.entries(album.shown ?? {});
  const changes = album.changes ?? [];
  const conflicted = album.conflicted ?? [];
  const foldShown = useFold(shown);
  const foldChanges = useFold(changes);
  return (
    <Stack gap={4} data-testid="gate-album">
      <Group gap="xs">
        <Text fw={500}>{album.label}</Text>
        {album.imported_by_run && <TokenBadge token="success">imported by this run</TokenBadge>}
      </Group>
      {album.message && <Text size="sm">{album.message}</Text>}
      {shown.length > 0 && (
        <Table fz="sm" withRowBorders={false} verticalSpacing={2} data-testid="gate-shown">
          <Table.Tbody>
            {foldShown.shown.map(([field, value]) => (
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
      {foldShown.control}
      {changes.length > 0 ? (
        <>
          <Table fz="sm" withRowBorders={false} verticalSpacing={2} data-testid="gate-changes">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Changed by this run</Table.Th>
                <Table.Th>Field</Table.Th>
                <Table.Th>Before → after</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {foldChanges.shown.map((c) => (
                <Table.Tr key={`${targetKey(c.target)}/${c.field}`}>
                  <Table.Td>
                    {c.target.scope === "album" ? "album" : `item ${c.target.id}`}
                  </Table.Td>
                  <Table.Td ff="monospace">{c.field}</Table.Td>
                  <Table.Td data-testid="gate-change">
                    <Diff
                      old={c.existed_before ? formatValue(c.before) : ""}
                      new={formatValue(c.after)}
                      empty="—"
                    />
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          {foldChanges.control}
        </>
      ) : (
        <Text size="xs" c="dimmed">
          No tag changes by this run so far.
        </Text>
      )}
      {conflicted.length > 0 && (
        <Text size="sm" style={{ color: tc("warning") }}>
          ⚠ Conflicted ChangeSets:{" "}
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
