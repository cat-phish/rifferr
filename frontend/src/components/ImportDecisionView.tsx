import {
  Alert,
  Anchor,
  Badge,
  Button,
  Group,
  Radio,
  Stack,
  Table,
  Text,
  TextInput,
} from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ok } from "../api/client";
import type { ImportCandidate, ImportDecision } from "../api/types";
import { BEETS_DOCS } from "../lib/beetsDocs";
import { time } from "../lib/format";

type Action = "candidate" | "asis" | "skip" | "keep" | "remove" | "merge";

/** What each duplicate choice does, in beets' terms (IMPORT-UI-3). */
const DUPLICATE_CHOICES: { action: Action; label: string; says: string }[] = [
  { action: "skip", label: "Skip", says: "Don't import the new album." },
  { action: "keep", label: "Keep both", says: "Import it next to the old one." },
  { action: "remove", label: "Remove old", says: "Replace the old album with the new one." },
  { action: "merge", label: "Merge", says: "Import the new tracks into the old album." },
];

export function seconds(length?: number | null): string {
  if (length === null || length === undefined) return "";
  const s = Math.round(length);
  return `${String(Math.floor(s / 60))}:${String(s % 60).padStart(2, "0")}`;
}

/** beets' distance as a percentage: 0 % is a perfect match. */
export function distancePercent(distance: number): string {
  return `${(distance * 100).toFixed(1)} %`;
}

/** Release ids from free text: split on commas and whitespace. */
export function parseIds(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter((s) => s !== "");
}

/** An import decision in the review queue (F-IMPORT-UI IMPORT-UI-15). */
export function ImportDecisionView({ decision }: { decision: ImportDecision }) {
  const queryClient = useQueryClient();
  const candidates = [...(decision.candidates ?? [])].sort((a, b) => a.distance - b.distance);
  const [selected, setSelected] = useState<string | null>(candidates[0]?.id ?? null);
  const [note, setNote] = useState("");
  const chosen = candidates.find((c) => c.id === selected) ?? null;
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["review"] });

  const decide = useMutation({
    mutationFn: async (action: Action) =>
      ok(
        await api.POST("/api/imports/{decision_id}/decision", {
          params: { path: { decision_id: decision.id } },
          body: {
            action,
            candidate_id: action === "candidate" ? selected : null,
            note: note.trim() === "" ? null : note.trim(),
          },
        }),
      ),
    onSuccess: refresh,
  });
  const lookup = useMutation({
    mutationFn: async (body: { ids?: string[]; artist?: string; album?: string }) =>
      ok(
        await api.POST("/api/imports/{decision_id}/lookup", {
          params: { path: { decision_id: decision.id } },
          body,
        }),
      ),
    onSuccess: refresh,
  });
  const busy = decide.isPending || lookup.isPending;

  return (
    <Stack gap="sm" data-testid="import-decision">
      <Text size="sm" ff="monospace" data-testid="import-path">
        {decision.path}
      </Text>
      {decision.deadline && (
        <Text size="sm" data-testid="deadline">
          If nobody decides by {time(decision.deadline)}, it will{" "}
          {decision.on_timeout === "asis" && decision.kind === "match" ? "import as-is" : "skip"}{" "}
          automatically.
        </Text>
      )}
      {decision.error && (
        <Alert color="red" data-testid="import-error">
          <Stack gap="xs">
            <Text size="sm">{decision.error}</Text>
            <Group>
              <Button
                size="xs"
                variant="light"
                loading={lookup.isPending}
                disabled={busy}
                onClick={() => {
                  lookup.mutate({});
                }}
              >
                Look up again
              </Button>
            </Group>
          </Stack>
        </Alert>
      )}
      <FolderItems decision={decision} />
      {decision.kind === "duplicate" ? (
        <Duplicates decision={decision} />
      ) : (
        <Candidates candidates={candidates} selected={selected} onSelect={setSelected} />
      )}
      {decision.kind === "match" && chosen && <TrackTable candidate={chosen} />}
      {decision.kind === "match" && (
        <Search
          busy={busy}
          pending={lookup.isPending}
          onSearch={(body) => {
            lookup.mutate(body);
          }}
        />
      )}
      {(decide.error ?? lookup.error) && (
        <Alert color="red">{(decide.error ?? lookup.error)?.message}</Alert>
      )}
      {lookup.isSuccess && (
        <Text size="sm" c="dimmed" data-testid="lookup-sent">
          Looking it up; new candidates appear here.
        </Text>
      )}
      {decide.isSuccess ? (
        <Text size="sm" c="dimmed" data-testid="decision-sent">
          {decide.variables === "skip" ? "Skipping it." : "Importing it."}
        </Text>
      ) : (
        <Group justify="space-between" align="flex-end">
          <TextInput
            size="xs"
            label="Note (optional)"
            value={note}
            onChange={(e) => {
              setNote(e.currentTarget.value);
            }}
            w={260}
          />
          <Group gap="xs">
            {decision.kind === "match" ? (
              <>
                <Button
                  size="xs"
                  disabled={busy || chosen === null}
                  loading={decide.isPending && decide.variables === "candidate"}
                  onClick={() => {
                    decide.mutate("candidate");
                  }}
                >
                  Import this match
                </Button>
                <Button
                  size="xs"
                  variant="light"
                  disabled={busy}
                  loading={decide.isPending && decide.variables === "asis"}
                  onClick={() => {
                    decide.mutate("asis");
                  }}
                >
                  Import as-is
                </Button>
                <Button
                  size="xs"
                  variant="light"
                  color="gray"
                  disabled={busy}
                  loading={decide.isPending && decide.variables === "skip"}
                  onClick={() => {
                    decide.mutate("skip");
                  }}
                >
                  Skip
                </Button>
              </>
            ) : (
              DUPLICATE_CHOICES.map((c) => (
                <Button
                  key={c.action}
                  size="xs"
                  variant={c.action === "skip" ? "light" : "filled"}
                  color={c.action === "remove" ? "red" : undefined}
                  title={c.says}
                  disabled={busy}
                  loading={decide.isPending && decide.variables === c.action}
                  onClick={() => {
                    decide.mutate(c.action);
                  }}
                >
                  {c.label}
                </Button>
              ))
            )}
          </Group>
        </Group>
      )}
      <Text size="xs" c="dimmed">
        How beets matches albums:{" "}
        <Anchor href={BEETS_DOCS.importer} target="_blank" rel="noreferrer" size="xs">
          the beets importer
        </Anchor>
      </Text>
    </Stack>
  );
}

function FolderItems({ decision }: { decision: ImportDecision }) {
  const items = decision.items ?? [];
  if (items.length === 0) return null;
  return (
    <Stack gap={4}>
      <Text size="sm" fw={500}>
        Files now
      </Text>
      <Table fz="sm" withRowBorders={false} data-testid="import-items">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>#</Table.Th>
            <Table.Th>Title</Table.Th>
            <Table.Th>Artist</Table.Th>
            <Table.Th>Album</Table.Th>
            <Table.Th>Length</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {items.map((item) => {
            const tags = item.tags ?? {};
            return (
              <Table.Tr key={item.path}>
                <Table.Td>{String(tags.track ?? "")}</Table.Td>
                <Table.Td>{String(tags.title ?? item.path.split("/").pop() ?? "")}</Table.Td>
                <Table.Td>{String(tags.artist ?? "")}</Table.Td>
                <Table.Td>{String(tags.album ?? "")}</Table.Td>
                <Table.Td>{seconds(item.length)}</Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}

function Candidates({
  candidates,
  selected,
  onSelect,
}: {
  candidates: ImportCandidate[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  if (candidates.length === 0) {
    return (
      <Text size="sm" c="dimmed" data-testid="no-candidates">
        beets found no candidates. Search by ID or by artist and album, or import as-is.
      </Text>
    );
  }
  return (
    <Radio.Group
      value={selected}
      onChange={onSelect}
      label="Candidates (lower distance is a closer match)"
    >
      <Stack gap={6} mt={4}>
        {candidates.map((c) => (
          <div key={c.id} data-testid="candidate" data-candidate={c.id}>
            <Radio
              value={c.id}
              label={
                <Group gap="xs" wrap="wrap">
                  <Text size="sm" fw={500}>
                    {c.artist} - {c.album}
                  </Text>
                  <Badge variant="light" size="sm">
                    distance {distancePercent(c.distance)}
                  </Badge>
                  <Text size="xs" c="dimmed">
                    {[
                      c.source,
                      c.year,
                      c.country,
                      c.label,
                      c.catalognum,
                      c.media,
                      `${String(c.tracks)} tracks`,
                    ]
                      .filter((v) => v !== null && v !== undefined && v !== "")
                      .join(" · ")}
                  </Text>
                  {c.url && (
                    <Anchor href={c.url} target="_blank" rel="noreferrer" size="xs">
                      open on {c.source}
                    </Anchor>
                  )}
                </Group>
              }
            />
          </div>
        ))}
      </Stack>
    </Radio.Group>
  );
}

function TrackTable({ candidate }: { candidate: ImportCandidate }) {
  const mapping = [...(candidate.mapping ?? [])].sort((a, b) => (a.track ?? 0) - (b.track ?? 0));
  const missing = candidate.missing ?? [];
  const unmatched = candidate.unmatched ?? [];
  return (
    <Stack gap={4} data-testid="track-table">
      <Table fz="sm" withRowBorders={false}>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>#</Table.Th>
            <Table.Th>Title now</Table.Th>
            <Table.Th>Candidate title</Table.Th>
            <Table.Th>Length</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {mapping.map((m) => (
            <Table.Tr key={m.item}>
              <Table.Td>{m.track ?? ""}</Table.Td>
              <Table.Td>{m.title_before ?? ""}</Table.Td>
              <Table.Td
                fw={m.title_before === m.title_after ? undefined : 500}
                c={m.title_before === m.title_after ? undefined : "blue"}
              >
                {m.title_after ?? ""}
              </Table.Td>
              <Table.Td>{seconds(m.length)}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      {missing.length > 0 && (
        <Text size="sm" c="orange" data-testid="missing-tracks">
          Missing tracks (no file): {missing.join(", ")}
        </Text>
      )}
      {unmatched.length > 0 && (
        <Text size="sm" c="orange" data-testid="unmatched-files">
          Unmatched files (no track): {unmatched.map((p) => p.split("/").pop()).join(", ")}
        </Text>
      )}
    </Stack>
  );
}

function Search({
  busy,
  pending,
  onSearch,
}: {
  busy: boolean;
  pending: boolean;
  onSearch: (body: { ids?: string[]; artist?: string; album?: string }) => void;
}) {
  const [ids, setIds] = useState("");
  const [artist, setArtist] = useState("");
  const [album, setAlbum] = useState("");
  return (
    <Stack gap="xs">
      <Group align="flex-end" gap="xs">
        <TextInput
          size="xs"
          label={
            <>
              Search by ID (
              <Anchor href={BEETS_DOCS.search_ids} target="_blank" rel="noreferrer" size="xs">
                search_ids
              </Anchor>
              )
            </>
          }
          placeholder="release ID or URL"
          value={ids}
          onChange={(e) => {
            setIds(e.currentTarget.value);
          }}
          w={320}
        />
        <Button
          size="xs"
          variant="light"
          disabled={busy || parseIds(ids).length === 0}
          loading={pending}
          onClick={() => {
            onSearch({ ids: parseIds(ids) });
          }}
        >
          Search by ID
        </Button>
      </Group>
      <Group align="flex-end" gap="xs">
        <TextInput
          size="xs"
          label="Artist"
          value={artist}
          onChange={(e) => {
            setArtist(e.currentTarget.value);
          }}
          w={200}
        />
        <TextInput
          size="xs"
          label="Album"
          value={album}
          onChange={(e) => {
            setAlbum(e.currentTarget.value);
          }}
          w={200}
        />
        <Button
          size="xs"
          variant="light"
          disabled={busy || (artist.trim() === "" && album.trim() === "")}
          loading={pending}
          onClick={() => {
            onSearch({ artist: artist.trim() || undefined, album: album.trim() || undefined });
          }}
        >
          Search by artist and album
        </Button>
      </Group>
    </Stack>
  );
}

function Duplicates({ decision }: { decision: ImportDecision }) {
  const albums = decision.duplicates ?? [];
  return (
    <Stack gap={4} data-testid="duplicates">
      <Text size="sm" fw={500}>
        The library already has {albums.length === 1 ? "this album" : "these albums"}:
      </Text>
      {albums.map((a) => (
        <Text key={a.id} size="sm">
          {[a.fields.albumartist, a.fields.album].filter(Boolean).map(String).join(" - ") || a.id}
          {a.path && (
            <Text span size="xs" c="dimmed" ff="monospace">
              {" "}
              {a.path}
            </Text>
          )}
        </Text>
      ))}
    </Stack>
  );
}
