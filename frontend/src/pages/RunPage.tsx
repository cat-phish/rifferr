import {
  Alert,
  Anchor,
  Badge,
  Button,
  Checkbox,
  Group,
  Modal,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link, useParams } from "react-router";
import { api, ok } from "../api/client";
import type { LaneOut, RunDetail, RunEvent } from "../api/types";
import { LogView } from "../components/LogView";
import { StatusBadge } from "../components/StatusBadge";
import { useEventStream } from "../hooks/useEventStream";
import { useProfile } from "../hooks/useProfile";
import { stepRows } from "../lib/callGroups";
import { duration } from "../lib/format";
import { count, formatValue, reviewLink, type Value } from "../lib/review";
import { applyEvent, initialView, isTerminal, type RunView } from "../lib/runState";

const RUN_EVENTS = [
  "run.started",
  "run.step.started",
  "run.step.log",
  "run.step.finished",
  "run.waiting_review",
  "run.resumed",
  "run.finished",
  "lane.started",
  "lane.waiting",
  "lane.resumed",
  "lane.finished",
] as const;

export function RunPage() {
  const { id = "" } = useParams();
  const queryClient = useQueryClient();
  const run = useQuery({
    queryKey: ["run", id],
    queryFn: async () =>
      ok(await api.GET("/api/runs/{run_id}", { params: { path: { run_id: id } } })),
  });
  if (run.error) return <Alert color="red">{run.error.message}</Alert>;
  if (run.data === undefined) return <Text c="dimmed">Loading…</Text>;
  return (
    <LiveRun
      key={id}
      run={run.data}
      onFinished={() => void queryClient.invalidateQueries({ queryKey: ["run", id] })}
    />
  );
}

/** Steps and live log for one run (UI-SHELL-7, -8). */
function LiveRun({ run, onFinished }: { run: RunDetail; onFinished: () => void }) {
  const [view, setView] = useState<RunView>(() => initialView(run));
  useEventStream<RunEvent>(`/api/runs/${run.id}/events`, {
    types: RUN_EVENTS,
    closeOn: "run.finished",
    onEvent: (type, event) => {
      setView((v) => applyEvent(v, event));
      if (type === "run.finished") onFinished();
    },
  });
  const cancel = useMutation({
    mutationFn: async () =>
      ok(await api.POST("/api/runs/{run_id}/cancel", { params: { path: { run_id: run.id } } })),
  });
  const active = !isTerminal(view.status);

  return (
    <Stack>
      <Group justify="space-between">
        <Group>
          <Title order={2}>{run.workflow ?? run.id}</Title>
          <StatusBadge status={view.status} />
          <Text c="dimmed" size="sm" data-testid="run-trigger">
            {run.trigger}
            {run.input ? `: ${run.input}` : ""}
          </Text>
          {isTerminal(view.status) && (
            <Text c="dimmed" size="sm">
              {duration(run.started_at, run.finished_at)}
            </Text>
          )}
        </Group>
        {active && (
          <Button
            color="red"
            variant="light"
            loading={cancel.isPending}
            disabled={cancel.isSuccess}
            onClick={() => {
              cancel.mutate();
            }}
          >
            {cancel.isSuccess ? "Cancelling…" : "Cancel"}
          </Button>
        )}
      </Group>
      {view.error !== null && <Alert color="red">{view.error}</Alert>}
      {view.status === "waiting_review" && <WaitingForReview runId={run.id} />}
      {isTerminal(view.status) && <UndoRun run={run} />}
      <Table data-testid="steps">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Step</Table.Th>
            <Table.Th>Type</Table.Th>
            <Table.Th>Status</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {stepRows(view.steps).map((row) =>
            row.kind === "call" ? (
              <Table.Tr key={`call:${row.id}`} data-testid={`call-${row.id}`}>
                <Table.Td colSpan={3} pl={row.depth * 16 + 8}>
                  <Text size="sm" fw={600}>
                    {row.label}
                    {row.workflow !== null && (
                      <Text span size="xs" c="dimmed" ff="monospace" ml="xs">
                        {row.workflow}
                      </Text>
                    )}
                  </Text>
                </Table.Td>
              </Table.Tr>
            ) : (
              <Table.Tr key={row.step.id} data-testid={`step-${row.step.id}`}>
                <Table.Td pl={row.depth * 16 + 8}>{row.step.id}</Table.Td>
                <Table.Td ff="monospace">{row.step.uses}</Table.Td>
                <Table.Td>
                  <StatusBadge status={row.step.status} />
                  <AIUsage outputs={row.step.outputs} />
                </Table.Td>
              </Table.Tr>
            ),
          )}
        </Table.Tbody>
      </Table>
      {view.lanes.length > 0 && <Lanes lanes={view.lanes} />}
      <LogView lines={view.log} />
    </Stack>
  );
}

const LANE_COLORS: Record<LaneOut["state"], string> = {
  active: "blue",
  waiting: "yellow",
  done: "green",
  failed: "red",
  rejected: "orange",
  gone: "gray",
  cancelled: "orange",
  skipped: "gray",
};

const HOLD_SAYS: Record<string, string> = {
  gate: "review gate",
  import: "import decision",
  cleanup: "cleanup decision",
  cover: "cover choice",
  move: "move conflict",
};

/** One row per album: where it is in the run and what it waits for (C-LANES LANES-15). */
function Lanes({ lanes }: { lanes: LaneOut[] }) {
  const { profile } = useProfile();
  return (
    <Stack gap="xs">
      <Title order={4}>Albums</Title>
      <Table data-testid="lanes">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Album</Table.Th>
            <Table.Th>State</Table.Th>
            <Table.Th>Step</Table.Th>
            <Table.Th>Waiting for</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {lanes.map((lane) => {
            const ran = Object.keys(lane.steps);
            const step = lane.position ?? ran[ran.length - 1] ?? "";
            return (
              <Table.Tr key={lane.key} data-testid={`lane-${lane.label}`}>
                <Table.Td>{lane.label}</Table.Td>
                <Table.Td>
                  <Badge color={LANE_COLORS[lane.state]} variant="light">
                    {lane.state}
                  </Badge>
                </Table.Td>
                <Table.Td ff="monospace">{step}</Table.Td>
                <Table.Td>
                  {lane.holds.map((hold, n) => (
                    <span key={hold.ref}>
                      {n > 0 && ", "}
                      <Anchor component={Link} to={reviewLink(profile, "item", hold.ref)}>
                        {HOLD_SAYS[hold.kind] ?? "tag changes"}
                      </Anchor>
                    </span>
                  ))}
                </Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}

/** A waiting run links to its items in the review queue (REVIEW-QUEUE-15). */
function WaitingForReview({ runId }: { runId: string }) {
  const { profile } = useProfile();
  return (
    <Alert color="blue" data-testid="waiting-review">
      Waiting for review.{" "}
      <Anchor component={Link} to={reviewLink(profile, "run", runId)}>
        Open its items in the review queue
      </Anchor>
    </Alert>
  );
}

interface UndoConflict {
  target: string;
  field: string;
  expected: Value;
  current: Value;
}

interface UndoReport {
  undone: string[];
  stopped_at: string | null;
  conflicts: UndoConflict[];
  problems: string[];
  /** Moved files changed since the move: undo refused them (LIBRARY-MOVE-15). */
  changed_files: string[];
  /** Files left alone so the only copy is never deleted (LIBRARY-MOVE-13, -14). */
  skipped_files: string[];
}

interface JobFinished {
  type: string;
  data: { job_id?: string; state?: string; error?: string | null; result?: unknown };
}

/** "Undo this run" for a finished run with applied changes (REVIEW-QUEUE-15, JOURNAL-6).
 * The undo job's report arrives as ``job.finished`` on ``/api/events``. */
function UndoRun({ run }: { run: RunDetail }) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [report, setReport] = useState<JobFinished["data"] | null>(null);
  // The job can finish before the POST that queued it returns.
  const finished = useRef<Record<string, JobFinished["data"]>>({});
  useEventStream<JobFinished>(jobId !== null || confirming ? "/api/events" : null, {
    types: ["job.finished"],
    onEvent: (_type, event) => {
      const id = event.data.job_id;
      if (id === undefined) return;
      finished.current[id] = event.data;
      if (id === jobId) {
        setReport(event.data);
        void queryClient.invalidateQueries({ queryKey: ["run", run.id] });
      }
    },
  });
  const undo = useMutation({
    mutationFn: async (force: string[]) =>
      ok(
        await api.POST("/api/runs/{run_id}/undo", {
          params: { path: { run_id: run.id } },
          body: { force },
        }),
      ),
    onSuccess: ({ job_id }) => {
      setConfirming(false);
      setReport(null);
      setJobId(job_id);
      const early = finished.current[job_id];
      if (early !== undefined) {
        setReport(early);
        void queryClient.invalidateQueries({ queryKey: ["run", run.id] });
      }
    },
  });
  const changes = run.undoable;
  if (changes === 0 && jobId === null) return null;
  return (
    <Stack gap="xs">
      {jobId === null && (
        <Group>
          <Button
            variant="light"
            color="orange"
            onClick={() => {
              setConfirming(true);
            }}
          >
            Undo this run
          </Button>
          <Text size="sm" c="dimmed">
            {count(changes, "applied change")} can be undone.
          </Text>
        </Group>
      )}
      {jobId !== null && report === null && (
        <Text size="sm" c="dimmed" data-testid="undo-pending">
          Undoing…
        </Text>
      )}
      {report !== null && (
        <UndoReportView
          data={report}
          forcing={undo.isPending}
          onForce={(files) => {
            undo.mutate(files);
          }}
        />
      )}
      <Modal
        opened={confirming}
        onClose={() => {
          setConfirming(false);
        }}
        title="Undo this run"
      >
        <Stack>
          <Text data-testid="undo-confirm">
            Undo the {count(changes, "tag change")} this run applied, newest first? The undo stops
            at the first field that changed again since; older changes then stay. Albums the run
            imported stay in the library.
          </Text>
          {undo.error && <Alert color="red">{undo.error.message}</Alert>}
          <Group justify="flex-end">
            <Button
              variant="default"
              onClick={() => {
                setConfirming(false);
              }}
            >
              Cancel
            </Button>
            <Button
              color="orange"
              loading={undo.isPending}
              onClick={() => {
                undo.mutate([]);
              }}
            >
              Undo
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}

function UndoReportView({
  data,
  forcing,
  onForce,
}: {
  data: JobFinished["data"];
  forcing: boolean;
  onForce: (files: string[]) => void;
}) {
  const result = data.result as Partial<UndoReport> | null | undefined;
  const undone = result?.undone?.length ?? 0;
  if (result?.stopped_at == null && data.state !== "succeeded") {
    return (
      <Alert color="red" data-testid="undo-report">
        Undo failed: {data.error ?? "unknown error"}
      </Alert>
    );
  }
  if (result?.stopped_at == null) {
    return (
      <Alert color="green" data-testid="undo-report">
        Undone: {count(undone, "change")} reverted.
      </Alert>
    );
  }
  return (
    <Alert color="orange" data-testid="undo-report">
      <Text size="sm">
        Reverted {count(undone, "change")}, then stopped at journal entry {result.stopped_at}: older
        changes stay.
      </Text>
      {(result.conflicts ?? []).map((c) => (
        <Text size="sm" key={`${c.target}/${c.field}`}>
          {c.target} {c.field}: expected {formatValue(c.expected)}, now {formatValue(c.current)}
        </Text>
      ))}
      {(result.problems ?? []).map((p) => (
        <Text size="sm" key={p}>
          {p}
        </Text>
      ))}
      <ChangedFiles files={result.changed_files ?? []} forcing={forcing} onForce={onForce} />
      {(result.skipped_files ?? []).length > 0 && (
        <Stack gap={2} mt="xs" data-testid="undo-skipped-files">
          <Text size="sm">Left alone, so the only copy is never deleted:</Text>
          {(result.skipped_files ?? []).map((f) => (
            <Text size="sm" ff="monospace" key={f}>
              {f}
            </Text>
          ))}
        </Stack>
      )}
    </Alert>
  );
}

/** Moved files that changed since the move (LIBRARY-MOVE-15): undo refused them. Each
 * can be undone anyway; it then moves back as it is now. */
function ChangedFiles({
  files,
  forcing,
  onForce,
}: {
  files: string[];
  forcing: boolean;
  onForce: (files: string[]) => void;
}) {
  const [picked, setPicked] = useState<Set<string>>(() => new Set());
  if (files.length === 0) return null;
  return (
    <Stack gap={4} mt="xs" data-testid="undo-changed-files">
      <Text size="sm">
        These library files changed since they were moved (their content is not what was moved), so
        the undo stopped. Undo anyway moves them back as they are now:
      </Text>
      {files.map((f) => (
        <Checkbox
          key={f}
          size="xs"
          label={
            <Text size="sm" ff="monospace">
              {f}
            </Text>
          }
          aria-label={`Undo ${f} anyway`}
          checked={picked.has(f)}
          disabled={forcing}
          onChange={() => {
            setPicked((s) => {
              const next = new Set(s);
              if (next.has(f)) next.delete(f);
              else next.add(f);
              return next;
            });
          }}
        />
      ))}
      <Group>
        <Button
          size="xs"
          color="orange"
          disabled={picked.size === 0}
          loading={forcing}
          onClick={() => {
            onForce([...picked]);
          }}
        >
          Undo anyway
        </Button>
      </Group>
    </Stack>
  );
}

/** Token totals of a step that asks an AI, and the cost when known (S-AI-TAG AI-TAG-11). */
function AIUsage({ outputs }: { outputs?: Record<string, unknown> | null }) {
  const input = outputs?.input_tokens;
  const output = outputs?.output_tokens;
  if (typeof input !== "number" || typeof output !== "number") return null;
  const cost = typeof outputs?.cost === "string" && outputs.cost !== "" ? outputs.cost : null;
  const currency = typeof outputs?.currency === "string" ? outputs.currency : "";
  return (
    <Text size="xs" c="dimmed" data-testid="ai-usage">
      {input} input / {output} output tokens
      {cost !== null ? `, ${cost} ${currency}`.trimEnd() : ""}
    </Text>
  );
}
