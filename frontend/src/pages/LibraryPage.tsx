import {
  Alert,
  Anchor,
  Badge,
  Button,
  Grid,
  Group,
  Modal,
  MultiSelect,
  SegmentedControl,
  Stack,
  Text,
  TextInput,
  Title,
  Autocomplete,
} from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import { ApiError, api, ok } from "../api/client";
import type { ProfileSummary } from "../api/types";
import { BeetsTerm } from "../components/BeetsTerm";
import { RecentEdits, type UndoResult } from "../components/RecentEdits";
import { type CellConflict, TagGrid } from "../components/TagGrid";
import { useEventStream } from "../hooks/useEventStream";
import { useProfile, withProfile } from "../hooks/useProfile";
import {
  type ColumnPrefs,
  DEFAULT_COLUMNS,
  DEFAULT_WIDTH,
  type Edit,
  type FieldValue,
  type Pending,
  type PendingTarget,
  type Row,
  type Scope,
  albumRow,
  cellKey,
  deleteEdit,
  itemRow,
  labelKey,
  listFields,
  loadPrefs,
  problemsByCell,
  savePrefs,
  setEdit,
  toRequest,
  withEdit,
} from "../lib/grid";

/** Queries and checks wait this long after the last keystroke or edit. */
export const GRID_DELAY_MS = 300;
const STREAM_EVENTS = [
  "changeset.proposed",
  "changeset.applied",
  "changeset.conflicted",
  "changeset.rejected",
  "changeset.undone",
  "job.finished",
] as const;

interface StreamEvent {
  seq: number;
  type: string;
  data: Record<string, unknown>;
}

interface Conflict extends CellConflict {
  row: Row;
  field: string;
}

/** Apply in progress: ChangeSet id -> the edits it carries (TAG-GRID-10). */
interface Inflight {
  job: string;
  behind: string[];
  sent: Record<string, PendingTarget[]>;
}

function clientId(): string {
  return `grid-${Math.random().toString(36).slice(2)}`;
}

function columnsFor(profile: ProfileSummary | undefined, scope: Scope): string[] {
  return profile?.grid_columns?.[scope] ?? DEFAULT_COLUMNS[scope];
}

/** The library browser and manual tag edits (F-TAG-GRID). */
export function LibraryPage() {
  const { profile, profiles } = useProfile();
  if (profile === null) return <Text c="dimmed">Choose a profile.</Text>;
  const summary = profiles.find((p) => p.name === profile);
  return <Library key={profile} profile={profile} summary={summary} />;
}

function Library({ profile, summary }: { profile: string; summary: ProfileSummary | undefined }) {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<Scope>("album");
  const [debouncedQuery] = useDebouncedValue(query, GRID_DELAY_MS);
  const [client] = useState(clientId);
  const [prefs, setPrefs] = useState<Record<Scope, ColumnPrefs>>(() => ({
    album: loadPrefs(profile, "album", columnsFor(summary, "album")),
    item: loadPrefs(profile, "item", columnsFor(summary, "item")),
  }));
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pending, setPending] = useState<Pending>({});
  const [conflicts, setConflicts] = useState<Conflict[]>([]);
  const [failures, setFailures] = useState<Map<string, string>>(new Map());
  const [inflight, setInflight] = useState<Inflight[]>([]);
  const [notice, setNotice] = useState<{ color: string; text: string } | null>(null);
  // Refs, not state: stream events can arrive before the POST that caused them returns,
  // and several can arrive before React re-renders.
  const flights = useRef<Inflight[]>([]);
  const undoJobs = useRef<Record<string, string>>({});
  const orphans = useRef<StreamEvent[]>([]);
  const [undoResults, setUndoResults] = useState<Record<string, UndoResult>>({});
  const [bulk, setBulk] = useState<"set" | "delete" | null>(null);

  const library = useQuery({
    queryKey: ["library", profile, debouncedQuery, scope],
    placeholderData: keepPreviousData,
    retry: false,
    queryFn: async ({ signal }) =>
      ok(
        await api.GET("/api/profiles/{profile}/library", {
          params: { path: { profile }, query: { query: debouncedQuery, scope, client } },
          signal,
        }),
      ),
  });
  const albums = useMemo(() => library.data?.albums ?? [], [library.data]);
  const lists = useMemo(() => listFields(albums), [albums]);
  const rows = useMemo(() => {
    const out = new Map<string, Row>();
    for (const a of albums) {
      if (scope === "album") out.set(albumRow(a).key, albumRow(a));
      for (const i of a.items ?? []) out.set(itemRow(a, i).key, itemRow(a, i));
    }
    return out;
  }, [albums, scope]);

  const busy = useMemo(
    () =>
      new Set(
        inflight.flatMap((f) =>
          Object.values(f.sent).flatMap((ts) => ts.map((t) => `${t.target.scope}:${t.target.id}`)),
        ),
      ),
    [inflight],
  );

  // -- check (TAG-GRID-7) -----------------------------------------------------------
  const request = useMemo(() => toRequest(pending), [pending]);
  const [debouncedRequest] = useDebouncedValue(request, GRID_DELAY_MS);
  const check = useQuery({
    queryKey: ["check", profile, debouncedRequest],
    enabled: debouncedRequest.length > 0,
    retry: false,
    queryFn: async ({ signal }) =>
      ok(
        await api.POST("/api/profiles/{profile}/changes/check", {
          params: { path: { profile } },
          body: { targets: debouncedRequest },
          signal,
        }),
      ),
  });
  const checked = check.data !== undefined && debouncedRequest === request && !check.isFetching;
  const problems = useMemo(
    () => (checked ? problemsByCell(check.data.problems) : new Map<string, string>()),
    [checked, check.data],
  );

  // -- apply (TAG-GRID-8, -13) --------------------------------------------------------
  const apply = useMutation({
    mutationFn: async (targets: PendingTarget[]) =>
      ok(
        await api.POST("/api/profiles/{profile}/changes", {
          params: { path: { profile } },
          body: { targets: toRequest(Object.fromEntries(targets.map((t) => [key(t), t]))) },
        }),
      ),
    onSuccess: (created, targets) => {
      // The server makes one ChangeSet per album, in the order albums first appear.
      const byAlbum = new Map<string, PendingTarget[]>();
      for (const t of targets) byAlbum.set(t.album, [...(byAlbum.get(t.album) ?? []), t]);
      const groups = [...byAlbum.values()];
      const sent: Record<string, PendingTarget[]> = {};
      created.changesets.forEach((id, n) => {
        sent[id] = groups[n] ?? [];
      });
      setFlights([
        ...flights.current,
        { job: created.job_id, behind: created.queued_behind, sent },
      ]);
      setNotice(null);
      const done = new Set(targets.map(key));
      setPending((p) => Object.fromEntries(Object.entries(p).filter(([k]) => !done.has(k))));
      replayOrphans(); // last: a refusal that already arrived puts edits back
    },
    onError: (err) => {
      setNotice({ color: "red", text: err.message });
    },
  });

  // -- outcomes from the profile stream (TAG-GRID-9, -10, -11) ----------------------------
  const setFlights = (next: Inflight[]) => {
    flights.current = next;
    setInflight(next);
  };
  const sentFor = (changesetId: string): PendingTarget[] | undefined =>
    flights.current.find((f) => changesetId in f.sent)?.sent[changesetId];
  const settle = (changesetId: string): PendingTarget[] => {
    const found = sentFor(changesetId) ?? [];
    setFlights(
      flights.current.map((f) => {
        if (!(changesetId in f.sent)) return f;
        return { ...f, sent: omit(f.sent, changesetId), behind: [] };
      }),
    );
    return found;
  };
  const restore = (targets: PendingTarget[]) => {
    if (targets.length === 0) return;
    setPending((p) => ({ ...p, ...Object.fromEntries(targets.map((t) => [key(t), t])) }));
  };

  /** Handle one event; false if it belongs to nothing this page is waiting for (yet). */
  const handle = (event: StreamEvent): boolean => {
    const d = event.data;
    const id = typeof d.changeset_id === "string" ? d.changeset_id : "";
    if (event.type === "changeset.applied" && sentFor(id) !== undefined) {
      settle(id);
      const failed = (d.write_failures ?? []) as { target: string; path: string; error: string }[];
      if (failed.length > 0) {
        setFailures((m) => {
          const next = new Map(m);
          for (const f of failed) next.set(labelKey(f.target), `tags not written: ${f.error}`);
          return next;
        });
      }
      return true;
    }
    if (event.type === "changeset.conflicted" && sentFor(id) !== undefined) {
      const sent = settle(id);
      const found = (d.conflicts ?? []) as { target: string; field: string; current: FieldValue }[];
      setConflicts((all) => [
        ...all,
        ...found.flatMap((c) => {
          const t = sent.find((x) => key(x) === labelKey(c.target));
          const wanted = t?.changes[c.field];
          if (t === undefined || wanted === undefined) return [];
          const row = rows.get(key(t)) ?? {
            key: key(t),
            target: t.target,
            album: t.album,
            fields: {},
          };
          return [{ row, field: c.field, current: c.current, wanted }];
        }),
      ]);
      return true;
    }
    if (event.type === "changeset.rejected" && sentFor(id) !== undefined) {
      restore(settle(id));
      const why = ((d.problems ?? []) as string[]).join("; ");
      setNotice({ color: "red", text: `Some edits were refused and are pending again: ${why}` });
      return true;
    }
    if (event.type !== "job.finished") return false;
    const job = typeof d.job_id === "string" ? d.job_id : "";
    const mine = flights.current.find((f) => f.job === job);
    if (mine !== undefined) {
      const left = Object.values(mine.sent).flat();
      setFlights(flights.current.filter((f) => f.job !== job));
      restore(left);
      if (d.state === "failed") {
        setNotice({ color: "red", text: `Applying failed: ${text(d.error)}` });
      } else if (left.length === 0) {
        setNotice({ color: "green", text: "Edits applied." });
      }
      return true;
    }
    const undone = undoJobs.current[job];
    if (undone !== undefined) {
      undoJobs.current = omit(undoJobs.current, job);
      setUndoResults((r) => ({ ...r, [undone]: undoResult(d) }));
      return true;
    }
    return false;
  };
  const replayOrphans = () => {
    orphans.current = orphans.current.filter((e) => !handle(e));
  };
  const onEvent = (_type: string, event: StreamEvent) => {
    void queryClient.invalidateQueries({ queryKey: ["recent-edits", profile] });
    if (event.type !== "changeset.proposed" && event.type !== "changeset.rejected") {
      void queryClient.invalidateQueries({ queryKey: ["library", profile] });
    }
    if (!handle(event)) orphans.current = [...orphans.current, event].slice(-200);
  };
  useEventStream<StreamEvent>(`/api/profiles/${encodeURIComponent(profile)}/events`, {
    types: STREAM_EVENTS,
    onEvent,
  });

  // -- columns (TAG-GRID-3) ----------------------------------------------------------
  useEffect(() => {
    savePrefs(profile, "album", prefs.album);
    savePrefs(profile, "item", prefs.item);
  }, [profile, prefs]);
  const seen = (s: Scope) =>
    (s === "album" ? library.data?.album_fields : library.data?.item_fields) ?? [];
  const choices = (s: Scope) => [...new Set([...prefs[s].columns, ...seen(s)])].sort();

  // -- edits ---------------------------------------------------------------------------
  const edit = (row: Row, field: string, e: Edit | null) => {
    setPending((p) => withEdit(p, row, field, e));
    setConflicts((all) => all.filter((c) => !(c.row.key === row.key && c.field === field)));
  };
  const selectedRows = [...selected].flatMap((k) => {
    const r = rows.get(k);
    return r === undefined ? [] : [r];
  });
  const conflictCells = new Map(conflicts.map((c) => [cellKey(c.row.key, c.field), c]));
  const pendingCount = request.reduce((n, t) => n + t.changes.length, 0);
  const canApply =
    pendingCount > 0 && checked && check.data.problems.length === 0 && !apply.isPending;
  const behind = inflight.flatMap((f) => f.behind);
  const scriptLink = `${withProfile("/scripts", profile)}&query=${encodeURIComponent(query)}`;

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>Library</Title>
        <Anchor component={Link} to={scriptLink} data-testid="script-link">
          Preview a script on these
        </Anchor>
      </Group>
      <Grid>
        <Grid.Col span={{ base: 12, lg: 9 }}>
          <Stack>
            <Group align="end">
              <TextInput
                style={{ flex: 1 }}
                label={
                  <>
                    Albums or items matching a <BeetsTerm concept="query">beets query</BeetsTerm>{" "}
                    (empty: the whole library)
                  </>
                }
                placeholder="albumartist:Beatles year:1965..1970"
                aria-label="Query"
                value={query}
                onChange={(e) => {
                  setQuery(e.currentTarget.value);
                }}
              />
              <SegmentedControl
                aria-label="Scope"
                value={scope}
                onChange={(v) => {
                  setScope(v === "item" ? "item" : "album");
                  setSelected(new Set());
                }}
                data={[
                  { value: "album", label: "Albums" },
                  { value: "item", label: "Items" },
                ]}
              />
            </Group>
            <Group align="end">
              {(["album", "item"] as const)
                .filter((s) => s === scope || (scope === "album" && expanded.size > 0))
                .map((s) => (
                  <MultiSelect
                    key={s}
                    style={{ flex: 1 }}
                    label={s === "album" ? "Album columns" : "Item columns"}
                    data={choices(s)}
                    value={prefs[s].columns}
                    onChange={(columns) => {
                      setPrefs((p) => ({ ...p, [s]: { ...p[s], columns } }));
                    }}
                    searchable
                  />
                ))}
            </Group>
            {library.error &&
              !(library.error instanceof ApiError && library.error.code === "superseded") && (
                <Alert color="red" data-testid="library-error">
                  {library.error.message}
                </Alert>
              )}
            <Group gap="xs">
              {library.data !== undefined && (
                <Text size="sm">
                  {albums.length} album{albums.length === 1 ? "" : "s"}
                </Text>
              )}
              {library.data?.truncated && (
                <Badge color="gray" variant="light">
                  more match; narrow the query
                </Badge>
              )}
              {library.isFetching && (
                <Text size="sm" c="dimmed">
                  Loading…
                </Text>
              )}
            </Group>
            <Group gap="xs">
              <Button
                size="xs"
                variant="default"
                disabled={selectedRows.length === 0}
                onClick={() => {
                  setBulk("set");
                }}
              >
                Set field…
              </Button>
              <Button
                size="xs"
                variant="default"
                disabled={selectedRows.length === 0}
                onClick={() => {
                  setBulk("delete");
                }}
              >
                Delete field…
              </Button>
              <Text size="xs" c="dimmed">
                {selectedRows.length} selected
              </Text>
            </Group>
            <TagGrid
              albums={albums}
              scope={scope}
              columns={{ album: prefs.album.columns, item: prefs.item.columns }}
              widths={{ album: prefs.album.widths, item: prefs.item.widths }}
              defaultWidth={DEFAULT_WIDTH}
              onResize={(s, field, width) => {
                setPrefs((p) => ({
                  ...p,
                  [s]: { ...p[s], widths: { ...p[s].widths, [field]: width } },
                }));
              }}
              expanded={expanded}
              onToggle={(id) => {
                setExpanded((e) => {
                  const next = new Set(e);
                  if (next.has(id)) next.delete(id);
                  else next.add(id);
                  return next;
                });
              }}
              selected={selected}
              onSelect={(changed, on) => {
                setSelected((sel) => {
                  const next = new Set(sel);
                  for (const r of changed) {
                    if (on) next.add(r.key);
                    else next.delete(r.key);
                  }
                  return next;
                });
              }}
              pending={pending}
              problems={problems}
              conflicts={conflictCells}
              failures={failures}
              busy={busy}
              lists={lists}
              onEdit={edit}
            />
          </Stack>
        </Grid.Col>
        <Grid.Col span={{ base: 12, lg: 3 }}>
          <Stack>
            <PendingPanel
              count={pendingCount}
              checking={pendingCount > 0 && !checked}
              problems={checked ? check.data.problems.length : 0}
              checkError={check.error?.message ?? null}
              canApply={canApply}
              applying={apply.isPending}
              onApply={() => {
                apply.mutate(Object.values(pending));
              }}
              onDiscard={() => {
                setPending({});
              }}
            />
            {behind.length > 0 && (
              <Alert color="blue" data-testid="queued" title="Queued">
                Your edits wait for{" "}
                {behind.map((run, n) => (
                  <span key={run}>
                    {n > 0 && ", "}
                    <Anchor component={Link} to={withProfile(`/runs/${run}`, profile)}>
                      run {run.slice(-6)}
                    </Anchor>
                  </span>
                ))}{" "}
                to finish; they apply right after.
              </Alert>
            )}
            {inflight.length > 0 && behind.length === 0 && (
              <Text size="sm" c="dimmed" data-testid="applying">
                Applying…
              </Text>
            )}
            {notice && (
              <Alert
                color={notice.color}
                data-testid="notice"
                withCloseButton
                onClose={() => {
                  setNotice(null);
                }}
              >
                {notice.text}
              </Alert>
            )}
            {conflicts.length > 0 && (
              <Alert color="orange" title="Changed since you loaded it" data-testid="conflicts">
                <Stack gap="xs">
                  {conflicts.map((c) => (
                    <Stack key={cellKey(c.row.key, c.field)} gap={4} data-testid="conflict">
                      <Text size="sm">
                        <code>{c.field}</code> of {c.row.key}: now{" "}
                        <b>{String(c.current ?? "(none)")}</b>
                      </Text>
                      <Group gap="xs">
                        <Button
                          size="xs"
                          variant="light"
                          onClick={() => {
                            const row: Row = {
                              ...c.row,
                              fields: { ...c.row.fields, [c.field]: c.current },
                            };
                            edit(row, c.field, { ...c.wanted, old: c.current });
                          }}
                        >
                          Apply on top
                        </Button>
                        <Button
                          size="xs"
                          variant="subtle"
                          onClick={() => {
                            setConflicts((all) => all.filter((x) => x !== c));
                          }}
                        >
                          Discard
                        </Button>
                      </Group>
                    </Stack>
                  ))}
                </Stack>
              </Alert>
            )}
            <RecentEdits
              profile={profile}
              results={undoResults}
              onUndo={async (changesetId, journalId) => {
                const r = await api.POST("/api/profiles/{profile}/journal/{entry_id}/undo", {
                  params: { path: { profile, entry_id: journalId } },
                });
                if (!r.response.ok) {
                  const e = new ApiError(r.response.status, r.error);
                  setUndoResults((u) => ({ ...u, [changesetId]: { ok: false, text: e.message } }));
                  return;
                }
                const { job_id } = ok(r);
                undoJobs.current[job_id] = changesetId;
                setUndoResults((u) => ({ ...u, [changesetId]: { ok: true, text: "Undoing…" } }));
                replayOrphans();
              }}
            />
          </Stack>
        </Grid.Col>
      </Grid>
      <BulkEdit
        mode={bulk}
        fields={choices(scope === "album" ? "album" : "item")}
        onClose={() => {
          setBulk(null);
        }}
        onApply={(field, value) => {
          setPending((p) =>
            selectedRows.reduce(
              (acc, row) =>
                withEdit(
                  acc,
                  row,
                  field,
                  bulk === "delete" ? deleteEdit(row, field) : setEdit(row, field, value, lists),
                ),
              p,
            ),
          );
          setBulk(null);
        }}
      />
    </Stack>
  );
}

const key = (t: PendingTarget): string => `${t.target.scope}:${t.target.id}`;

const text = (value: unknown): string => (typeof value === "string" ? value : "");

function omit<T>(record: Record<string, T>, name: string): Record<string, T> {
  return Object.fromEntries(Object.entries(record).filter(([k]) => k !== name));
}

function undoResult(d: Record<string, unknown>): UndoResult {
  if (d.state === "succeeded") return { ok: true, text: "Undone." };
  const result = (d.result ?? {}) as {
    conflicts?: { target: string; field: string; current: unknown; would_restore: unknown }[];
    problems?: string[];
  };
  const details = [
    ...(result.conflicts ?? []).map(
      (c) =>
        `${c.field} of ${c.target} is now ${JSON.stringify(c.current)}; undo would restore ${JSON.stringify(c.would_restore)}`,
    ),
    ...(result.problems ?? []),
  ];
  const why = details.length > 0 ? details.join("; ") : text(d.error) || "failed";
  return { ok: false, text: `Not undone: ${why}` };
}

function PendingPanel(props: {
  count: number;
  checking: boolean;
  problems: number;
  checkError: string | null;
  canApply: boolean;
  applying: boolean;
  onApply: () => void;
  onDiscard: () => void;
}) {
  if (props.count === 0) {
    return (
      <Text size="sm" c="dimmed">
        Double-click a cell (or press Enter on it) to edit. Edits stay pending until you apply them.
      </Text>
    );
  }
  return (
    <Stack gap="xs" data-testid="pending">
      <Text size="sm">
        {props.count} pending edit{props.count === 1 ? "" : "s"}
      </Text>
      {props.checking && (
        <Text size="xs" c="dimmed">
          Checking…
        </Text>
      )}
      {props.problems > 0 && (
        <Text size="xs" c="red" data-testid="problems">
          {props.problems} problem{props.problems === 1 ? "" : "s"}: fix or discard the red cells
        </Text>
      )}
      {props.checkError && (
        <Text size="xs" c="red">
          {props.checkError}
        </Text>
      )}
      <Group gap="xs">
        <Button
          size="xs"
          disabled={!props.canApply}
          loading={props.applying}
          onClick={props.onApply}
        >
          Apply
        </Button>
        <Button size="xs" variant="default" onClick={props.onDiscard}>
          Discard all
        </Button>
      </Group>
    </Stack>
  );
}

/** "Set field" / "Delete field" on the selected rows (TAG-GRID-6). Values are literal. */
function BulkEdit({
  mode,
  fields,
  onClose,
  onApply,
}: {
  mode: "set" | "delete" | null;
  fields: string[];
  onClose: () => void;
  onApply: (field: string, value: string) => void;
}) {
  const [field, setField] = useState("");
  const [value, setValue] = useState("");
  const valid = /^[a-z_][a-z0-9_]*$/.test(field);
  return (
    <Modal
      opened={mode !== null}
      onClose={onClose}
      title={
        mode === "delete"
          ? "Delete a field on the selected rows"
          : "Set a field on the selected rows"
      }
    >
      <Stack>
        <Autocomplete
          label="Field"
          data={fields}
          value={field}
          onChange={setField}
          error={field && !valid ? "a beets field name: lowercase letters, digits, _" : undefined}
        />
        {mode === "set" && (
          <TextInput
            label="Value"
            description="Literal text; templates belong in tag scripts"
            value={value}
            onChange={(e) => {
              setValue(e.currentTarget.value);
            }}
          />
        )}
        <Group justify="end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!valid}
            onClick={() => {
              onApply(field, value);
              setField("");
              setValue("");
            }}
          >
            Add pending edits
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
