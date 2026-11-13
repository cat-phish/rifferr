import {
  Alert,
  Anchor,
  Badge,
  Button,
  Group,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { api, ok } from "../api/client";
import { useProfile, withProfile } from "../hooks/useProfile";

const NAME = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

/** The profile's tag-script files (TAGSCRIPT-22, API-18). */
export function ScriptsPage() {
  const { profile } = useProfile();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  // A query handed over from the library grid (TAG-GRID-12) carries on to the editor.
  const query = params.get("query");
  const link = (to: string) =>
    query === null
      ? withProfile(to, profile)
      : `${withProfile(to, profile)}&query=${encodeURIComponent(query)}`;
  const [name, setName] = useState("");
  const scripts = useQuery({
    queryKey: ["scripts", profile],
    enabled: profile !== null,
    queryFn: async () =>
      ok(
        await api.GET("/api/profiles/{profile}/scripts", {
          params: { path: { profile: profile ?? "" } },
        }),
      ),
  });
  const valid = NAME.test(name);
  return (
    <Stack>
      <Title order={2}>Tag scripts</Title>
      <Text size="sm" c="dimmed">
        Script files in this profile&apos;s <code>scripts/</code> folder. A <code>tags.script</code>{" "}
        step runs one and proposes the changes for review.
      </Text>
      {query !== null && query !== "" && (
        <Text size="sm" data-testid="handed-query">
          Open a script to preview it on <code>{query}</code>.
        </Text>
      )}
      {scripts.error && <Alert color="red">{scripts.error.message}</Alert>}
      {scripts.data?.length === 0 && <Text c="dimmed">No script files yet.</Text>}
      {scripts.data !== undefined && scripts.data.length > 0 && (
        <Table highlightOnHover data-testid="scripts">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Name</Table.Th>
              <Table.Th>File</Table.Th>
              <Table.Th>Status</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {scripts.data.map((s) => (
              <Table.Tr key={s.name}>
                <Table.Td>
                  <Anchor component={Link} to={link(`/scripts/${s.name}`)}>
                    {s.name}
                  </Anchor>
                </Table.Td>
                <Table.Td>
                  <Text size="sm" ff="monospace">
                    {s.file}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Badge color={s.valid ? "green" : "red"} variant="light">
                    {s.valid ? "valid" : "invalid"}
                  </Badge>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
      <Group align="end">
        <TextInput
          label="New script"
          placeholder="release-types"
          value={name}
          onChange={(e) => {
            setName(e.currentTarget.value);
          }}
          error={name && !valid ? "letters, digits, - and _ only" : undefined}
        />
        <Button
          disabled={!valid}
          onClick={() => {
            void navigate(link(`/scripts/${name}`));
          }}
        >
          Create
        </Button>
      </Group>
    </Stack>
  );
}
