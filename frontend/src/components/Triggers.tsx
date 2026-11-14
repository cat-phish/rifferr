import { Alert, Badge, Button, Code, Group, Paper, Stack, Table, Text, Title } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api, ok } from "../api/client";
import type { TriggerState, WebhookSecret } from "../api/types";
import { withProfile } from "../hooks/useProfile";
import { time } from "../lib/format";

const STATE_COLOR: Record<TriggerState["state"], string> = {
  armed: "green",
  disabled: "yellow",
  error: "red",
};

/** This profile's triggers (TRIGGERS-4), refreshed by ``trigger.*`` events. */
export function useTriggers(profile: string) {
  return useQuery({
    queryKey: ["triggers", profile],
    queryFn: async () =>
      ok(
        await api.GET("/api/profiles/{profile}/triggers", {
          params: { path: { profile } },
        }),
      ),
  });
}

function describe(t: TriggerState): string {
  if (t.kind === "watch") return t.path ?? "";
  if (t.kind === "schedule") return t.next_fire ? `next: ${time(t.next_fire)}` : "";
  return t.has_secret ? "secret set" : "no secret yet";
}

/**
 * The workflow's ``on:`` triggers and their state (F-TRIGGERS). A webhook's secret is
 * created here and shown once (TRIGGERS-17).
 */
export function Triggers({ profile, workflow }: { profile: string; workflow: string }) {
  const queryClient = useQueryClient();
  const triggers = useTriggers(profile);
  const [secret, setSecret] = useState<WebhookSecret | null>(null);
  const create = useMutation({
    mutationFn: async (name: string) =>
      ok(
        await api.POST("/api/profiles/{profile}/webhooks/{name}/secret", {
          params: { path: { profile, name } },
        }),
      ),
    onSuccess: async (created) => {
      setSecret(created);
      await queryClient.invalidateQueries({ queryKey: ["triggers", profile] });
    },
  });
  const mine = (triggers.data ?? []).filter((t) => t.workflow === workflow);
  if (mine.length === 0) return null;

  return (
    <Paper withBorder p="md" data-testid="triggers">
      <Stack>
        <Title order={3}>Triggers</Title>
        <Table>
          <Table.Tbody>
            {mine.map((t) => (
              <Table.Tr key={t.kind} data-testid={`trigger-${t.kind}`}>
                <Table.Td>{t.kind}</Table.Td>
                <Table.Td>
                  <Badge color={STATE_COLOR[t.state]} variant="light">
                    {t.state}
                  </Badge>
                  {t.detail && (
                    <Text size="xs" c="dimmed">
                      {t.detail}
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>
                  <Text size="sm" ff={t.kind === "watch" ? "monospace" : undefined}>
                    {describe(t)}
                  </Text>
                </Table.Td>
                <Table.Td>
                  {t.last_run_id && (
                    <Text size="sm">
                      last:{" "}
                      <Link to={withProfile(`/runs/${t.last_run_id}`, profile)}>
                        {time(t.last_fired)}
                      </Link>
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>
                  {t.webhook_name && (
                    <Button
                      size="xs"
                      variant="light"
                      loading={create.isPending}
                      onClick={() => {
                        if (t.webhook_name) create.mutate(t.webhook_name);
                      }}
                    >
                      {t.has_secret ? "Replace secret" : "Create secret"}
                    </Button>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
        {create.error && <Alert color="red">{create.error.message}</Alert>}
        {secret !== null && (
          <Alert color="blue" title="Webhook secret" data-testid="webhook-secret">
            <Stack gap="xs">
              <Text size="sm">
                <Code>POST {secret.url_path}</Code> with{" "}
                <Code>Authorization: Bearer &lt;secret&gt;</Code> (or the secret as the HTTP Basic
                password). It is shown only now; Rifferr keeps only a hash of it.
              </Text>
              <Code block>{secret.secret}</Code>
              <Group>
                <Button
                  size="xs"
                  variant="subtle"
                  onClick={() => {
                    setSecret(null);
                  }}
                >
                  Done
                </Button>
              </Group>
            </Stack>
          </Alert>
        )}
      </Stack>
    </Paper>
  );
}
