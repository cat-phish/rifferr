import { Anchor, Stack, Table, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, ok } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";
import { useProfile, withProfile } from "../hooks/useProfile";
import { duration, time } from "../lib/format";

/** Recent runs, refreshed live from /api/events by the app shell (UI-SHELL-9). */
export function RunsPage() {
  const { profile } = useProfile();
  const runs = useQuery({
    queryKey: ["runs", profile],
    enabled: profile !== null,
    queryFn: async () =>
      ok(await api.GET("/api/runs", { params: { query: { profile, limit: 50 } } })),
  });

  return (
    <Stack>
      <Title order={2}>Runs</Title>
      {runs.data?.length === 0 && (
        <Text c="dimmed">
          No runs yet. Start one from{" "}
          <Anchor component={Link} to={withProfile("/workflows", profile)}>
            Workflows
          </Anchor>
          .
        </Text>
      )}
      {runs.data !== undefined && runs.data.length > 0 && (
        <Table highlightOnHover data-testid="runs">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Workflow</Table.Th>
              <Table.Th>State</Table.Th>
              <Table.Th>Started</Table.Th>
              <Table.Th>Duration</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {runs.data.map((r) => (
              <Table.Tr key={r.id}>
                <Table.Td>
                  <Anchor component={Link} to={withProfile(`/runs/${r.id}`, profile)}>
                    {r.workflow ?? r.id}
                  </Anchor>
                </Table.Td>
                <Table.Td>
                  <StatusBadge status={r.state} />
                </Table.Td>
                <Table.Td>{time(r.started_at ?? r.created_at)}</Table.Td>
                <Table.Td>{duration(r.started_at, r.finished_at)}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}
