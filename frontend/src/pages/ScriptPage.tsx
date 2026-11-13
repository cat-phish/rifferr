import {
  Alert,
  Badge,
  Button,
  Grid,
  Group,
  Stack,
  Table,
  Text,
  TextInput,
  Textarea,
  Title,
} from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams, useSearchParams } from "react-router";
import { ApiError, api, ok } from "../api/client";
import type { PreviewField, PreviewResponse, ScriptDetail } from "../api/types";
import { BeetsTerm } from "../components/BeetsTerm";
import { ErrorList } from "../components/ErrorList";
import { useProfile } from "../hooks/useProfile";

/** Preview waits this long after the last edit (TAGSCRIPT-22). */
export const PREVIEW_DELAY_MS = 300;
const PREVIEW_LIMIT = 50;

const NEW_SCRIPT = `# A tag script: actions run in order on each album and its items.
# Templates and conditions use beets syntax ($field, beets queries).
scope: album
actions:
  - name: example
    when: 'albumtype:ep'
    set: { albumdisambig: 'EP' }
`;

type Value = PreviewField["before"];

function show(value: Value, deleted = false): string {
  if (deleted || value === null) return "(absent)";
  if (Array.isArray(value)) return value.join("; ");
  return String(value);
}

/** One random id per editor, so a newer preview supersedes this editor's older one. */
function clientId(): string {
  return `editor-${Math.random().toString(36).slice(2)}`;
}

/** Edit a script file as YAML with a live preview grid (TAGSCRIPT-22). */
export function ScriptPage() {
  const { name = "" } = useParams();
  const { profile } = useProfile();
  const loaded = useQuery({
    queryKey: ["script", profile, name],
    enabled: profile !== null,
    retry: false,
    queryFn: async (): Promise<ScriptDetail | null> => {
      const r = await api.GET("/api/profiles/{profile}/scripts/{name}", {
        params: { path: { profile: profile ?? "", name } },
      });
      if (r.response.status === 404) return null; // a new script
      return ok(r);
    },
  });
  if (loaded.error) return <Alert color="red">{loaded.error.message}</Alert>;
  if (loaded.data === undefined || profile === null) return <Text c="dimmed">Loading…</Text>;
  return <Editor key={`${profile}/${name}`} profile={profile} name={name} file={loaded.data} />;
}

function Editor({
  profile,
  name,
  file,
}: {
  profile: string;
  name: string;
  file: ScriptDetail | null;
}) {
  const queryClient = useQueryClient();
  const [params] = useSearchParams();
  const [text, setText] = useState(file?.text ?? NEW_SCRIPT);
  const [sha, setSha] = useState<string | null>(file?.sha256 ?? null);
  const [savedText, setSavedText] = useState(file?.text ?? null);
  const [query, setQuery] = useState(() => params.get("query") ?? ""); // from the grid (TAG-GRID-12)
  const [conflict, setConflict] = useState(false);
  const [client] = useState(clientId);
  const [debouncedText] = useDebouncedValue(text, PREVIEW_DELAY_MS);
  const [debouncedQuery] = useDebouncedValue(query, PREVIEW_DELAY_MS);

  const preview = useQuery({
    queryKey: ["preview", profile, debouncedText, debouncedQuery],
    placeholderData: keepPreviousData,
    retry: false,
    queryFn: async ({ signal }) =>
      ok(
        await api.POST("/api/profiles/{profile}/tagscript/preview", {
          params: { path: { profile } },
          body: { script_text: debouncedText, query: debouncedQuery, limit: PREVIEW_LIMIT, client },
          signal,
        }),
      ),
  });

  const save = useMutation({
    mutationFn: async () =>
      ok(
        await api.PUT("/api/profiles/{profile}/scripts/{name}", {
          params: { path: { profile, name } },
          body: { text, sha256: sha },
        }),
      ),
    onSuccess: async (saved) => {
      setSha(saved.sha256);
      setSavedText(saved.text);
      setConflict(false);
      await queryClient.invalidateQueries({ queryKey: ["scripts", profile] });
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "hash_mismatch") setConflict(true);
    },
  });

  const reload = async (): Promise<void> => {
    const fresh = ok(
      await api.GET("/api/profiles/{profile}/scripts/{name}", {
        params: { path: { profile, name } },
      }),
    );
    setText(fresh.text);
    setSha(fresh.sha256);
    setSavedText(fresh.text);
    setConflict(false);
    queryClient.setQueryData(["script", profile, name], fresh);
  };

  const dirty = text !== savedText;
  const superseded = preview.error instanceof ApiError && preview.error.code === "superseded";
  return (
    <Stack>
      <Group>
        <Title order={2}>{name}</Title>
        {file === null && savedText === null && <Badge variant="light">new</Badge>}
        {dirty && (
          <Badge color="yellow" variant="light">
            unsaved
          </Badge>
        )}
      </Group>
      <Grid>
        <Grid.Col span={{ base: 12, lg: 5 }}>
          <Stack>
            <Textarea
              label="Script (YAML)"
              aria-label="Script"
              data-testid="script-text"
              value={text}
              onChange={(e) => {
                setText(e.currentTarget.value);
              }}
              rows={20}
              spellCheck={false}
              styles={{ input: { fontFamily: "var(--mantine-font-family-monospace)" } }}
            />
            <Group>
              <Button
                onClick={() => {
                  save.mutate();
                }}
                loading={save.isPending}
                disabled={!dirty}
              >
                Save
              </Button>
              {save.isSuccess && !dirty && (
                <Text size="sm" c="dimmed">
                  Saved
                </Text>
              )}
            </Group>
            {conflict && (
              <Alert color="yellow" title="The file changed on disk">
                <Stack gap="xs">
                  <Text size="sm">
                    Someone changed this script since you opened it. Reload it to see their version
                    (your edits here are replaced).
                  </Text>
                  <Group>
                    <Button size="xs" variant="light" onClick={() => void reload()}>
                      Reload
                    </Button>
                  </Group>
                </Stack>
              </Alert>
            )}
            {save.error && !conflict && <Alert color="red">{save.error.message}</Alert>}
          </Stack>
        </Grid.Col>
        <Grid.Col span={{ base: 12, lg: 7 }}>
          <Stack>
            <TextInput
              label={
                <>
                  Preview albums matching a <BeetsTerm concept="query">beets query</BeetsTerm>
                </>
              }
              placeholder="albumtype:ep"
              aria-label="Query"
              value={query}
              onChange={(e) => {
                setQuery(e.currentTarget.value);
              }}
            />
            {preview.error && !superseded && (
              <Alert color="red" data-testid="preview-error">
                {preview.error.message}
              </Alert>
            )}
            {preview.data !== undefined && <PreviewGrid data={preview.data} />}
          </Stack>
        </Grid.Col>
      </Grid>
    </Stack>
  );
}

const key = (target: string, field: string): string => `${target}\u0000${field}`;

/** Before/after per target and field; second-pass differences in red (TAGSCRIPT-22). */
export function PreviewGrid({ data }: { data: PreviewResponse }) {
  const issues = data.idempotence ?? [];
  const errors = data.validation_errors ?? [];
  const flagged = new Set(issues.map((i) => key(i.target.id, i.field)));
  const responsible = new Set(issues.map((i) => i.action).filter((a): a is string => a !== null));
  const targets = (data.targets ?? []).filter(
    (t) => (t.fields ?? []).length > 0 || (t.errors ?? []).length > 0 || (t.notes ?? []).length > 0,
  );
  if (errors.length > 0) {
    return (
      <Alert color="red" title="The script has errors" data-testid="preview-validation">
        <ErrorList errors={errors} />
      </Alert>
    );
  }
  return (
    <Stack gap="xs" data-testid="preview">
      <Group gap="xs">
        <Text size="sm">
          {data.albums} album{data.albums === 1 ? "" : "s"} evaluated
        </Text>
        {data.truncated && (
          <Badge color="gray" variant="light">
            more albums match
          </Badge>
        )}
        {data.idempotence_checked && issues.length === 0 && (
          <Badge color="green" variant="light">
            idempotent
          </Badge>
        )}
      </Group>
      {issues.length > 0 && (
        <Alert color="red" title="A second pass would change more" data-testid="idempotence">
          {issues.map((i) => (
            <Text size="sm" key={key(i.target.id, i.field)}>
              <b>{i.action ?? "an action"}</b> changes {i.field} of {i.target.id} again:{" "}
              <code>{show(i.first)}</code> → <code>{show(i.second)}</code>
            </Text>
          ))}
        </Alert>
      )}
      {targets.length === 0 ? (
        <Text c="dimmed" size="sm">
          No changes.
        </Text>
      ) : (
        <Table withTableBorder data-testid="preview-grid">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Target</Table.Th>
              <Table.Th>Field</Table.Th>
              <Table.Th>Before</Table.Th>
              <Table.Th>After</Table.Th>
              <Table.Th>Actions</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {targets.flatMap((t) => [
              ...(t.fields ?? []).map((f) => {
                const bad = flagged.has(key(t.target.id, f.field));
                return (
                  <Table.Tr key={key(t.target.id, f.field)} data-testid="preview-row">
                    <Table.Td>{t.label}</Table.Td>
                    <Table.Td ff="monospace">{f.field}</Table.Td>
                    <Table.Td>{show(f.before)}</Table.Td>
                    <Table.Td
                      data-changed="true"
                      data-flagged={bad ? "true" : undefined}
                      bg={
                        bad ? "var(--mantine-color-red-light)" : "var(--mantine-color-yellow-light)"
                      }
                    >
                      {show(f.after, f.deleted)}
                    </Table.Td>
                    <Table.Td>
                      <Group gap={4}>
                        {f.actions.map((a) => (
                          <Badge
                            key={a}
                            size="sm"
                            variant={responsible.has(a) ? "filled" : "light"}
                            color={responsible.has(a) ? "red" : "blue"}
                            data-responsible={responsible.has(a) ? "true" : undefined}
                          >
                            {a}
                          </Badge>
                        ))}
                      </Group>
                    </Table.Td>
                  </Table.Tr>
                );
              }),
              ...(t.errors ?? []).map((e, i) => (
                <Table.Tr key={`${t.target.id}-error-${String(i)}`}>
                  <Table.Td>{t.label}</Table.Td>
                  <Table.Td colSpan={4} c="red">
                    {e}
                  </Table.Td>
                </Table.Tr>
              )),
              ...(t.notes ?? []).map((n, i) => (
                <Table.Tr key={`${t.target.id}-note-${String(i)}`}>
                  <Table.Td>{t.label}</Table.Td>
                  <Table.Td colSpan={4} c="dimmed">
                    {n}
                  </Table.Td>
                </Table.Tr>
              )),
            ])}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}
