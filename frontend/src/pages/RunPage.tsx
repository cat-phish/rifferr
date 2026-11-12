import { Alert, Button, Group, Stack, Table, Text, Title } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router";
import { api, ok } from "../api/client";
import type { RunDetail, RunEvent } from "../api/types";
import { LogView } from "../components/LogView";
import { StatusBadge } from "../components/StatusBadge";
import { useEventStream } from "../hooks/useEventStream";
import { duration } from "../lib/format";
import { applyEvent, initialView, isTerminal, type RunView } from "../lib/runState";

const RUN_EVENTS = [
  "run.started",
  "run.step.started",
  "run.step.log",
  "run.step.finished",
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
