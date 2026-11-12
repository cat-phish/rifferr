import { Alert, Anchor, Badge, Group, Stack, Table, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, ok } from "../api/client";
import { BeetsTerm } from "../components/BeetsTerm";
import { useProfile, withProfile } from "../hooks/useProfile";

/** The profile's workflows with validity and trust status (UI-SHELL-3, -4). */
export function WorkflowsPage() {
  const { profile } = useProfile();
  const workflows = useQuery({
    queryKey: ["workflows", profile],
    enabled: profile !== null,
    queryFn: async () =>
      ok(await api.GET("/api/workflows", { params: { query: { profile: profile ?? "" } } })),
  });

  return (
    <Stack>
      <Title order={2}>Workflows</Title>
      <Text size="sm" c="dimmed">
        Workflow files in this profile&apos;s <code>workflows/</code> folder. Their steps work on
        your <BeetsTerm concept="library">beets library</BeetsTerm>.
      </Text>
      {workflows.error && <Alert color="red">{workflows.error.message}</Alert>}
      {workflows.data?.length === 0 && <Text c="dimmed">No workflow files yet.</Text>}
      {workflows.data !== undefined && workflows.data.length > 0 && (
        <Table highlightOnHover data-testid="workflows">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Name</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>Vars to set</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {workflows.data.map((w) => (
              <Table.Tr key={w.path}>
                <Table.Td>
                  <Anchor component={Link} to={withProfile(`/workflows/${w.name}`, profile)}>
                    {w.name}
                  </Anchor>
                </Table.Td>
                <Table.Td>
                  <Group gap="xs">
                    <Badge color={w.valid ? "green" : "red"} variant="light">
                      {w.valid ? "valid" : "invalid"}
                    </Badge>
                    <Badge color={w.trusted ? "green" : "yellow"} variant="light">
                      {w.trusted ? "trusted" : "needs review"}
                    </Badge>
                  </Group>
                </Table.Td>
                <Table.Td>
                  <Text size="sm" ff="monospace">
                    {w.missing_vars?.join(", ")}
                  </Text>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}
