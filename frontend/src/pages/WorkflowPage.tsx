import {
  Alert,
  Badge,
  Button,
  Code,
  Group,
  Paper,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router";
import { ApiError, api, ok } from "../api/client";
import type { WorkflowDetail } from "../api/types";
import { ErrorList } from "../components/ErrorList";
import { SafetyReport } from "../components/SafetyReport";
import { Triggers } from "../components/Triggers";
import { useProfile, withProfile } from "../hooks/useProfile";

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
      <Group>
        <Title order={2}>{wf.name}</Title>
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
      <Start wf={wf} profile={profile} />
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
  const trust = useMutation({
    mutationFn: async () =>
      ok(
        await api.POST("/api/workflows/{name}/trust", {
          params: { path: { name: wf.name }, query: { profile } },
          body: { sha256: wf.sha256 },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["workflow", profile, wf.name] });
      await queryClient.invalidateQueries({ queryKey: ["workflows", profile] });
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
            Trust this workflow
          </Button>
        </Group>
      </Stack>
    </Paper>
  );
}

/** Vars form and Start (UI-SHELL-5, -6). Never asks about trust. */
function Start({ wf, profile }: { wf: WorkflowDetail; profile: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>({});
  const [input, setInput] = useState("");
  const missing = new Set(wf.missing_vars ?? []);
  const vars = wf.vars ?? [];
  const reason = !wf.valid ? "Fix the errors first." : !wf.trusted ? "Review it first." : null;
  const unset = [...missing].filter((n) => !(values[n] ?? "").trim());
  const start = useMutation({
    mutationFn: async () => {
      const filled = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ""));
      return ok(
        await api.POST("/api/runs", {
          body: {
            profile,
            workflow: wf.name,
            vars: filled,
            sha256: wf.sha256,
            ...(input.trim() ? { input: input.trim() } : {}),
          },
        }),
      );
    },
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
      await navigate(withProfile(`/runs/${created.id}`, profile));
    },
    onError: async (err) => {
      if (err instanceof ApiError && err.code === "hash_mismatch") {
        await queryClient.invalidateQueries({ queryKey: ["workflow", profile, wf.name] });
      }
    },
  });

  return (
    <Paper withBorder p="md" data-testid="start">
      <Stack>
        <Title order={3}>Start</Title>
        {vars.map((v) => (
          <TextInput
            key={v.name}
            label={v.name}
            description={v.description}
            required={missing.has(v.name)}
            placeholder={v.profile_value ?? v.default ?? ""}
            value={values[v.name] ?? ""}
            onChange={(e) => {
              const value = e.currentTarget.value;
              setValues((prev) => ({ ...prev, [v.name]: value }));
            }}
          />
        ))}
        <TextInput
          label="Input folder"
          description="Absolute path for {{ run.input }}; leave empty for the workflow's input"
          placeholder="/path/to/Artist - Album"
          value={input}
          onChange={(e) => {
            setInput(e.currentTarget.value);
          }}
        />
        {start.error && (
          <Alert color="red">
            {start.error instanceof ApiError && start.error.code === "hash_mismatch"
              ? "The file changed since it was shown; it has been reloaded."
              : start.error.message}
          </Alert>
        )}
        <Group>
          <Button
            onClick={() => {
              start.mutate();
            }}
            loading={start.isPending}
            disabled={reason !== null || unset.length > 0}
          >
            Start run
          </Button>
          {reason !== null && (
            <Text size="sm" c="dimmed">
              {reason}
            </Text>
          )}
          {reason === null && unset.length > 0 && (
            <Text size="sm" c="dimmed">
              Set {unset.join(", ")}.
            </Text>
          )}
        </Group>
      </Stack>
    </Paper>
  );
}
