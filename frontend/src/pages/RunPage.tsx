import { Alert, Anchor, Button, Group, Modal, Stack, Table, Text, Title } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link, useParams } from "react-router";
import { api, ok } from "../api/client";
import type { RunDetail, RunEvent } from "../api/types";
import { LogView } from "../components/LogView";
import { StatusBadge } from "../components/StatusBadge";
import { useEventStream } from "../hooks/useEventStream";
import { useProfile } from "../hooks/useProfile";
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
          {view.steps.map((s) => (
            <Table.Tr key={s.id} data-testid={`step-${s.id}`}>
              <Table.Td>{s.id}</Table.Td>
              <Table.Td ff="monospace">{s.uses}</Table.Td>
              <Table.Td>
                <StatusBadge status={s.status} />
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <LogView lines={view.log} />
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
    mutationFn: async () =>
      ok(await api.POST("/api/runs/{run_id}/undo", { params: { path: { run_id: run.id } } })),
    onSuccess: ({ job_id }) => {
      setConfirming(false);
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
      {report !== null && <UndoReportView data={report} />}
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
                undo.mutate();
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

function UndoReportView({ data }: { data: JobFinished["data"] }) {
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
    </Alert>
  );
}
