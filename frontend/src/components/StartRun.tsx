import { Alert, Button, Group, Paper, Stack, Text, TextInput, Title } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router";
import { ApiError, api, ok } from "../api/client";
import type { WorkflowDetail } from "../api/types";
import { withProfile } from "../hooks/useProfile";

/** Vars form and Start (UI-SHELL-5, -6). Never asks about trust. On the Details tab it opens
 * the new run; the workflow page header passes `onStarted` to open Pipeline (UI-REFRESH-25). */
export function StartRun({
  wf,
  profile,
  onStarted,
  framed = true,
}: {
  wf: WorkflowDetail;
  profile: string;
  onStarted?: (runId: string) => void;
  framed?: boolean;
}) {
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
      await queryClient.invalidateQueries({ queryKey: ["pipeline"] });
      if (onStarted) onStarted(created.id);
      else await navigate(withProfile(`/runs/${created.id}`, profile));
    },
    onError: async (err) => {
      if (err instanceof ApiError && err.code === "hash_mismatch") {
        await queryClient.invalidateQueries({ queryKey: ["workflow", profile, wf.name] });
      }
    },
  });

  const body = (
    <Stack>
      {framed && <Title order={3}>Start</Title>}
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
  );
  return framed ? (
    <Paper withBorder p="md" data-testid="start">
      {body}
    </Paper>
  ) : (
    <div data-testid="start-dialog">{body}</div>
  );
}
