import { Alert, Badge, Button, Code, Group, Paper, Stack, Text, Title } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router";
import { ApiError, api, ok } from "../api/client";
import type { WorkflowDetail } from "../api/types";
import { ErrorList } from "../components/ErrorList";
import { SafetyReport } from "../components/SafetyReport";
import { StartRun } from "../components/StartRun";
import { Triggers } from "../components/Triggers";
import { useProfile } from "../hooks/useProfile";

export function WorkflowPage() {
  const { name = "" } = useParams();
  const { profile } = useProfile();
  const detail = useQuery({
    queryKey: ["workflow", profile, name],
    enabled: profile !== null,
    queryFn: async () =>
      ok(
        await api.GET("/api/workflows/{name}", {
          params: { path: { name }, query: { profile: profile ?? "" } },
        }),
      ),
  });
  if (detail.error) return <Alert color="red">{detail.error.message}</Alert>;
  if (detail.data === undefined || profile === null) return <Text c="dimmed">Loading…</Text>;
  const wf = detail.data;
  return (
    <Stack>
      {/* The name is the title of the workflow's page around this tab (UI-REFRESH-23). */}
      <Group>
        <Badge color={wf.valid ? "green" : "red"} variant="light">
          {wf.valid ? "valid" : "invalid"}
        </Badge>
        <Badge color={wf.trusted ? "green" : "yellow"} variant="light">
          {wf.trusted ? "trusted" : "needs review"}
        </Badge>
      </Group>
      <Text size="sm" c="dimmed" ff="monospace">
        {wf.path}
      </Text>
      {wf.errors.length > 0 && (
        <Alert color="red" title="Validation errors">
          <ErrorList errors={wf.errors} />
        </Alert>
      )}
      {!wf.trusted && <Review wf={wf} profile={profile} />}
      <Triggers profile={profile} workflow={wf.name} />
      <StartRun wf={wf} profile={profile} />
    </Stack>
  );
}

/**
 * Import review (UI-SHELL-4): YAML, hash and the full safety report, then an explicit
 * Trust that names the hash shown. A changed file reloads the review instead.
 */
function Review({ wf, profile }: { wf: WorkflowDetail; profile: string }) {
  const queryClient = useQueryClient();
  const [changed, setChanged] = useState(false);
  const report = useQuery({
    queryKey: ["safety", profile, wf.name, wf.sha256],
    queryFn: async () =>
      ok(
        await api.POST("/api/workflows/{name}/validate", {
          params: { path: { name: wf.name }, query: { profile } },
        }),
      ),
  });
  const reload = async (): Promise<void> => {
    setChanged(true);
    await queryClient.invalidateQueries({ queryKey: ["workflow", profile, wf.name] });
  };
  // SUBWORKFLOW-13: each called file is trusted on its own, by the hash shown here.
  const untrusted = (wf.called ?? []).filter((c) => !c.trusted);
  const trust = useMutation({
    mutationFn: async () =>
      ok(
        await api.POST("/api/workflows/{name}/trust", {
          params: { path: { name: wf.name }, query: { profile } },
          body:
            untrusted.length === 0
              ? { sha256: wf.sha256 }
              : {
                  sha256: wf.sha256,
                  called: Object.fromEntries(untrusted.map((c) => [c.path, c.sha256])),
                },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["workflow", profile, wf.name] });
      await queryClient.invalidateQueries({ queryKey: ["workflows", profile] });
      // Trusting arms the workflow's triggers (TRIGGERS-2); arming sends no event.
      await queryClient.invalidateQueries({ queryKey: ["triggers", profile] });
    },
    onError: async (err) => {
      if (err instanceof ApiError && err.code === "hash_mismatch") await reload();
    },
  });
  const stale = report.data !== undefined && report.data.sha256 !== wf.sha256;

  return (
    <Paper withBorder p="md" data-testid="review">
      <Stack>
        <Title order={3}>Review before trusting</Title>
        <Text size="sm">
          This workflow file came from outside Rifferr. Read it and its safety report. Trusting it
          lets its shell steps run this exact content; any edit needs a new review.
        </Text>
        <Code block>{wf.yaml}</Code>
        <Text size="sm">
          Content hash: <Code>{wf.sha256}</Code>
        </Text>
        {untrusted.map((c) => (
          <Stack key={c.path} gap="xs" data-testid={`called-${c.path}`}>
            <Title order={4}>Called workflow {c.path}</Title>
            <Text size="sm">
              This workflow calls it, and it isn&apos;t trusted yet. Trusting covers it too, by its
              own hash.
            </Text>
            <Code block>{c.yaml}</Code>
            <Text size="sm">
              Content hash: <Code>{c.sha256}</Code>
            </Text>
          </Stack>
        ))}
        <Title order={4}>Safety report</Title>
        {report.data !== undefined && <SafetyReport findings={report.data.safety} />}
        {(changed || stale) && (
          <Alert color="yellow">The file changed since it was shown. Review it again.</Alert>
        )}
        {trust.error &&
          !(trust.error instanceof ApiError && trust.error.code === "hash_mismatch") && (
            <Alert color="red">{trust.error.message}</Alert>
          )}
        <Group>
          <Button
            onClick={() => {
              trust.mutate();
            }}
            loading={trust.isPending}
            disabled={report.data === undefined || stale}
          >
            {untrusted.length === 0
              ? "Trust this workflow"
              : `Trust this workflow and ${String(untrusted.length)} called file${untrusted.length === 1 ? "" : "s"}`}
          </Button>
        </Group>
      </Stack>
    </Paper>
  );
}
