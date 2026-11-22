import {
  Alert,
  Anchor,
  Button,
  Grid,
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
import { webUrl } from "../lib/webUrl";
import { time } from "../lib/format";
import { tc } from "../theme";
import { Diff } from "./Diff";
import { useFold } from "./Fold";
import { HeaderActions } from "./HeaderActions";
import { MatchBadge, Penalties, type MatchThresholds } from "./MatchBadge";
import { NoteField } from "./NoteField";
import { jobOf, useMarkDecided } from "./Decided";

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

/** beets' distance as a percentage: 0 % is a perfect match. The card shows similarity
 * instead (UI-REFRESH-6, `similarityPercent`). */
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

/** An import decision in the review queue (F-IMPORT-UI IMPORT-UI-15; layout and colors
 * F-UI-REFRESH UI-REFRESH-6 .. -9): candidates on the left, the selected one's track
 * table on the right, the main actions in the item's header. */
export function ImportDecisionView({ decision }: { decision: ImportDecision }) {
  const queryClient = useQueryClient();
  const markDecided = useMarkDecided();
  const candidates = [...(decision.candidates ?? [])].sort((a, b) => a.distance - b.distance);
  const [selected, setSelected] = useState<string | null>(candidates[0]?.id ?? null);
  const [note, setNote] = useState("");
  const chosen = candidates.find((c) => c.id === selected) ?? null;
  const thresholds = decision.match_thresholds ?? null;
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
    onSuccess: (res) => {
      markDecided(jobOf(res));
      refresh();
    },
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
    <Stack gap={6} data-testid="import-decision">
      <Group gap="xs" wrap="wrap">
        <Text size="sm" ff="monospace" data-testid="import-path">
          {decision.path}
        </Text>
        {decision.deadline && (
          <Text size="xs" c="dimmed" data-testid="deadline">
            If nobody decides by {time(decision.deadline)}, it will{" "}
            {decision.on_timeout === "asis" && decision.kind === "match" ? "import as-is" : "skip"}{" "}
            automatically.
          </Text>
        )}
      </Group>
      {decision.error && (
        <Alert color="red" data-testid="import-error" p="xs">
          <Group gap="xs" justify="space-between">
            <Text size="sm">{decision.error}</Text>
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
        </Alert>
      )}
      <FolderItems decision={decision} />
      {decision.kind === "duplicate" ? (
        <Duplicates decision={decision} />
      ) : (
        <Grid gap="md">
          <Grid.Col span={{ base: 12, md: chosen ? 5 : 12 }}>
            <Stack gap={6}>
              <Candidates
                candidates={candidates}
                selected={selected}
                onSelect={setSelected}
                thresholds={thresholds}
                lookupProblem={decision.lookup_problem ?? null}
              />
              <Search
                busy={busy}
                pending={lookup.isPending}
                onSearch={(body) => {
                  lookup.mutate(body);
                }}
              />
            </Stack>
          </Grid.Col>
          {chosen && (
            <Grid.Col span={{ base: 12, md: 7 }}>
              <TrackTable candidate={chosen} thresholds={thresholds} />
            </Grid.Col>
          )}
        </Grid>
      )}
      {(decide.error ?? lookup.error) && (
        <Alert color="red">{(decide.error ?? lookup.error)?.message}</Alert>
      )}
      {lookup.isSuccess && (
        <Text size="sm" c="dimmed" data-testid="lookup-sent">
          Looking it up; new candidates appear here.
        </Text>
      )}
      <Group justify="space-between" gap="xs">
        {decide.isSuccess ? <span /> : <NoteField value={note} onChange={setNote} />}
        <Text size="xs" c="dimmed">
          How beets matches albums:{" "}
          <Anchor href={BEETS_DOCS.importer} target="_blank" rel="noreferrer" size="xs">
            the beets importer
          </Anchor>
        </Text>
      </Group>
      <HeaderActions>
        {decide.isSuccess ? (
          <Text size="sm" c="dimmed" data-testid="decision-sent">
            {decide.variables === "skip" ? "Skipping it." : "Importing it."}
          </Text>
        ) : decision.kind === "match" ? (
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
      </HeaderActions>
    </Stack>
  );
}

/** The folder's files as they are now, collapsed by default (UI-REFRESH-9). */
function FolderItems({ decision }: { decision: ImportDecision }) {
  const items = decision.items ?? [];
  const [open, setOpen] = useState(false);
  const fold = useFold(items);
  if (items.length === 0) return null;
  return (
    <Stack gap={2}>
      <Group>
        <Button
          variant="subtle"
          size="compact-xs"
          aria-expanded={open}
          onClick={() => {
            setOpen((o) => !o);
          }}
        >
          {open
            ? "Hide the folder's files"
            : `Show the folder's ${String(items.length)} file${items.length === 1 ? "" : "s"}`}
        </Button>
      </Group>
      {open && (
        <>
          <Table fz="sm" withRowBorders={false} verticalSpacing={2} data-testid="import-items">
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
              {fold.shown.map((item) => {
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
          {fold.control}
        </>
      )}
    </Stack>
  );
}

function Candidates({
  candidates,
  selected,
  onSelect,
  thresholds,
  lookupProblem,
}: {
  candidates: ImportCandidate[];
  selected: string | null;
  onSelect: (id: string) => void;
  thresholds: MatchThresholds | null;
  lookupProblem: string | null;
}) {
  const fold = useFold(candidates);
  if (candidates.length === 0 && lookupProblem) {
    // IMPORT-UI-20: say why there can't be candidates, not just that there are none
    return (
      <Alert color="yellow" data-testid="lookup-problem">
        <Text size="sm">{lookupProblem}</Text>
      </Alert>
    );
  }
  if (candidates.length === 0) {
    return (
      <Text size="sm" c="dimmed" data-testid="no-candidates">
        beets found no candidates. Search by ID or by artist and album, or import as-is.
      </Text>
    );
  }
  return (
    <Radio.Group value={selected} onChange={onSelect} label="Candidates (best match first)">
      <Stack gap={4} mt={4}>
        {fold.shown.map((c) => (
          <div key={c.id} data-testid="candidate" data-candidate={c.id}>
            <Radio
              value={c.id}
              size="xs"
              label={
                <Stack gap={0}>
                  <Group gap="xs" wrap="wrap">
                    <MatchBadge distance={c.distance} thresholds={thresholds} />
                    <Text size="sm" fw={500}>
                      {c.artist} - {c.album}
                    </Text>
                  </Group>
                  <Group gap="xs" wrap="wrap">
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
                    {webUrl(c.url) && (
                      <Anchor
                        href={webUrl(c.url) ?? undefined}
                        target="_blank"
                        rel="noreferrer"
                        size="xs"
                      >
                        open on {c.source}
                      </Anchor>
                    )}
                  </Group>
                  <Penalties names={c.penalties ?? []} />
                </Stack>
              }
            />
          </div>
        ))}
        {fold.control}
      </Stack>
    </Radio.Group>
  );
}

const WARNING = { color: tc("warning") } as const;

/** beets' distance keys as words: `data_source` → "data source". */
function words(key: string): string {
  return key.replace(/_/g, " ");
}

/** Track penalties the table's columns already show; the rest are listed per row. */
const SHOWN_TRACK_PENALTIES = new Set([
  "track_title",
  "track_length",
  "track_index",
  "track_artist",
]);

/** Album fields beets compares, now and on the candidate (UI-REFRESH-18). */
function AlbumFields({ candidate }: { candidate: ImportCandidate }) {
  const fields = candidate.fields ?? [];
  if (fields.length === 0) return null;
  return (
    <Table fz="sm" withRowBorders={false} verticalSpacing={2} data-testid="album-fields">
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Album field</Table.Th>
          <Table.Th>Now</Table.Th>
          <Table.Th>Candidate</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {fields.map((f) => (
          <Table.Tr
            key={f.field}
            data-testid="album-field"
            data-field={f.field}
            data-penalized={f.penalized ? "true" : undefined}
          >
            <Table.Td
              style={f.penalized ? WARNING : undefined}
              c={f.penalized ? undefined : "dimmed"}
            >
              {f.penalized ? "⚠ " : ""}
              {words(f.field)}
            </Table.Td>
            <Table.Td>
              <Diff old={f.current ?? ""} new={f.candidate ?? ""} side="old" empty="—" />
            </Table.Td>
            <Table.Td>
              <Diff old={f.current ?? ""} new={f.candidate ?? ""} side="new" empty="—" />
            </Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
}

function TrackTable({
  candidate,
  thresholds,
}: {
  candidate: ImportCandidate;
  thresholds: MatchThresholds | null;
}) {
  const mapping = [...(candidate.mapping ?? [])].sort((a, b) => (a.track ?? 0) - (b.track ?? 0));
  const missing = candidate.missing ?? [];
  const unmatched = candidate.unmatched ?? [];
  const fold = useFold(mapping);
  // An artist column only when beets sees a track artist differ (various artists).
  const artists = mapping.some((m) => (m.penalties ?? []).includes("track_artist"));
  return (
    <Stack gap={4} data-testid="track-table">
      {/* The chosen candidate's match, above its comparison (UI-REFRESH-18). */}
      <Group gap="xs" wrap="wrap" data-testid="chosen-match">
        <MatchBadge distance={candidate.distance} thresholds={thresholds} />
        <Text size="sm" fw={500}>
          {candidate.artist} - {candidate.album}
        </Text>
      </Group>
      <AlbumFields candidate={candidate} />
      <Table fz="sm" withRowBorders={false} verticalSpacing={2}>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>#</Table.Th>
            <Table.Th>Title now</Table.Th>
            <Table.Th>Candidate title</Table.Th>
            {artists && <Table.Th>Artist</Table.Th>}
            <Table.Th>Length</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {fold.shown.map((m) => {
            const before = m.title_before ?? "";
            const after = m.title_after ?? "";
            const penalties = m.penalties ?? [];
            const has = (key: string) => penalties.includes(key);
            const other = penalties.filter((p) => !SHOWN_TRACK_PENALTIES.has(p));
            return (
              <Table.Tr
                key={m.item}
                data-testid="track"
                data-changed={before === after && penalties.length === 0 ? undefined : "true"}
                data-penalties={penalties.join(" ")}
              >
                <Table.Td>
                  {has("track_index") && m.track_before !== null && m.track_before !== undefined ? (
                    <Diff old={String(m.track_before)} new={String(m.track ?? "")} empty="" />
                  ) : (
                    <Text span fz="sm" c="dimmed">
                      {m.track ?? ""}
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>
                  <Diff old={before} new={after} side="old" empty="" />
                </Table.Td>
                <Table.Td>
                  <Diff old={before} new={after} side="new" empty="" />
                  {other.length > 0 && (
                    <Text span fz="xs" style={WARNING} data-testid="track-penalties">
                      {" "}
                      ⚠ {other.map(words).join(", ")}
                    </Text>
                  )}
                </Table.Td>
                {artists && (
                  <Table.Td>
                    <Diff old={m.artist_before ?? ""} new={m.artist_after ?? ""} empty="" />
                  </Table.Td>
                )}
                <Table.Td data-testid="track-length">
                  {has("track_length") ? (
                    <Text span fz="sm" style={WARNING}>
                      ⚠ {seconds(m.length_before)} → {seconds(m.length)}
                    </Text>
                  ) : (
                    <Text span fz="sm" c="dimmed">
                      {seconds(m.length)}
                    </Text>
                  )}
                </Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
      {fold.control}
      {missing.length > 0 && (
        <Text size="sm" style={WARNING} data-testid="missing-tracks">
          ⚠ Missing tracks (no file): {missing.join(", ")}
        </Text>
      )}
      {unmatched.length > 0 && (
        <Text size="sm" style={WARNING} data-testid="unmatched-files">
          ⚠ Unmatched files (no track): {unmatched.map((p) => p.split("/").pop()).join(", ")}
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
    <Group align="flex-end" gap="xs" wrap="wrap">
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
          w={220}
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
          w={150}
        />
        <TextInput
          size="xs"
          label="Album"
          value={album}
          onChange={(e) => {
            setAlbum(e.currentTarget.value);
          }}
          w={150}
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
    </Group>
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
