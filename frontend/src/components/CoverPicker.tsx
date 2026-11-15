import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Checkbox,
  FileButton,
  Group,
  Image,
  Modal,
  NumberInput,
  SimpleGrid,
  Slider,
  Stack,
  Switch,
  Text,
  TextInput,
  UnstyledButton,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError, api, ok } from "../api/client";
import type { CoverProvidersView, CoverQuery, CoverReview, RankedCandidate } from "../api/types";
import { useEventStream } from "../hooks/useEventStream";
import {
  FILTER_LABEL,
  IMAGE_TYPES,
  PASTE_LINK_MESSAGE,
  type ProviderRow,
  albumArt,
  bytesLabel,
  candidateImage,
  imagesFrom,
  providerRows,
  rankCandidates,
  sizeLabel,
  toBase64,
} from "../lib/covers";

/** Where the picker is used: a cover review in the queue, or a library album outside a
 * run (F-COVER-PICKER COVER-PICKER-1, -2). */
export type PickerMode =
  { kind: "review"; review: CoverReview } | { kind: "album"; albumId: string; label: string };

interface ProviderStatus {
  name: string;
  state: "searching" | "done" | "failed" | "skipped";
  count: number;
  cached: boolean;
  message?: string | null;
}

interface CoverEvent {
  type: string;
  data: {
    search_id?: string;
    candidate?: RankedCandidate;
    status?: ProviderStatus;
    ranked?: RankedCandidate[];
    hidden?: RankedCandidate[];
    dropped?: Record<string, Record<string, number>>;
    cancelled?: boolean;
    error?: string;
    job_id?: string;
    state?: string;
    changeset_id?: string;
  };
}

const STREAM_TYPES = [
  "covers.candidate",
  "covers.provider",
  "covers.done",
  "changeset.applied",
  "changeset.conflicted",
  "changeset.failed",
  "job.finished",
] as const;

type QueryFields = Pick<
  CoverQuery,
  "artist" | "album" | "year" | "catalognum" | "barcode" | "mb_albumid" | "mb_releasegroupid"
>;

const EMPTY_QUERY: QueryFields = { artist: "", album: "" };

function fields(q: CoverQuery): QueryFields {
  return {
    artist: q.artist,
    album: q.album,
    year: q.year ?? null,
    catalognum: q.catalognum ?? null,
    barcode: q.barcode ?? null,
    mb_albumid: q.mb_albumid ?? null,
    mb_releasegroupid: q.mb_releasegroupid ?? null,
  };
}

/** The cover picker: an editable query, a grid that fills as providers answer, provider
 * toggles and weights, a full-size preview, and the user's own images by upload, drop or
 * paste (F-COVER-PICKER). */
export function CoverPicker({ profile, mode }: { profile: string; mode: PickerMode }) {
  const albumId = mode.kind === "album" ? mode.albumId : null;
  const albumQuery = useQuery({
    queryKey: ["cover-query", profile, albumId],
    enabled: albumId !== null,
    queryFn: async () =>
      ok(
        await api.GET("/api/profiles/{profile}/albums/{album_id}/cover-query", {
          params: { path: { profile, album_id: albumId ?? "" } },
        }),
      ),
  });
  const view = useQuery({
    queryKey: ["cover-providers", profile],
    queryFn: async () =>
      ok(await api.GET("/api/profiles/{profile}/covers", { params: { path: { profile } } })),
  });
  const error = albumQuery.error ?? view.error;
  if (error) return <Alert color="red">{error.message}</Alert>;
  if (view.data === undefined || (albumId !== null && albumQuery.data === undefined)) {
    return (
      <Text size="sm" c="dimmed" data-testid="cover-picker-loading">
        Loading…
      </Text>
    );
  }
  return (
    <PickerBody
      profile={profile}
      mode={mode}
      initialQuery={albumQuery.data?.query}
      currentArt={albumQuery.data?.art_sha256 ?? null}
      view={view.data}
    />
  );
}

function PickerBody({
  profile,
  mode,
  initialQuery,
  currentArt,
  view,
}: {
  profile: string;
  mode: PickerMode;
  initialQuery: CoverQuery | undefined;
  currentArt: string | null;
  view: CoverProvidersView;
}) {
  const queryClient = useQueryClient();
  const review = mode.kind === "review" ? mode.review : null;
  const albumId = mode.kind === "album" ? mode.albumId : null;

  // -- the query (COVER-PICKER-2, -4) -------------------------------------------------------
  const [query, setQuery] = useState<QueryFields>(() =>
    review !== null
      ? fields(review.query)
      : initialQuery !== undefined
        ? fields(initialQuery)
        : EMPTY_QUERY,
  );
  const currentSha = review !== null ? (review.old_sha256 ?? null) : currentArt;

  // -- providers (COVER-PICKER-9 to -11) ----------------------------------------------------
  const [rows, setProviders] = useState<ProviderRow[]>(() =>
    providerRows(view.providers, view.registered),
  );
  const [hash, setHash] = useState(view.profile_hash);

  // -- candidates and searches (COVER-PICKER-5 to -8) ---------------------------------------
  const [candidates, setCandidates] = useState<Map<string, RankedCandidate>>(
    () => new Map((review?.candidates ?? []).map((c) => [c.id, c])),
  );
  const [statuses, setStatuses] = useState<Record<string, ProviderStatus>>({});
  const [dropped, setDropped] = useState<Record<string, Record<string, number>>>({});
  const [searchNote, setSearchNote] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const active = useRef(new Set<string>());
  const running = useRef(new Set<string>());
  const buffered = useRef(new Map<string, CoverEvent[]>());
  const asked = useRef(new Set<string>());
  const autoStarted = useRef(false);

  const upsert = (list: RankedCandidate[]) => {
    setCandidates((all) => {
      const next = new Map(all);
      for (const c of list) next.set(c.id, c);
      return next;
    });
  };

  const apply = (event: CoverEvent) => {
    const d = event.data;
    if (event.type === "covers.candidate" && d.candidate) upsert([d.candidate]);
    else if (event.type === "covers.provider" && d.status) {
      const s = d.status;
      setStatuses((all) => ({ ...all, [s.name]: s }));
    } else if (event.type === "covers.done") {
      if (d.search_id) running.current.delete(d.search_id);
      upsert([...(d.ranked ?? []), ...(d.hidden ?? [])]);
      if (d.dropped) {
        const more = d.dropped;
        setDropped((all) => ({ ...all, ...more }));
      }
      if (d.error) setSearchNote(`The search failed: ${d.error}`);
      else if (d.cancelled) setSearchNote("The search was cancelled.");
    }
  };

  const cancelRunning = () => {
    for (const id of running.current) {
      void api.DELETE("/api/covers/search/{search_id}", {
        params: { path: { search_id: id } },
      });
    }
    running.current.clear();
  };

  const startSearch = async (names: string[], opts: { fresh: boolean; refresh?: boolean }) => {
    if (names.length === 0) return;
    if (opts.fresh) {
      cancelRunning();
      active.current.clear();
      asked.current.clear();
      setCandidates((all) => new Map([...all].filter(([, c]) => c.provider === "upload")));
      setStatuses({});
      setDropped({});
      setSearchNote(null);
    }
    for (const n of names) asked.current.add(n);
    setStatuses((all) => {
      const next = { ...all };
      for (const n of names) next[n] = { name: n, state: "searching", count: 0, cached: false };
      return next;
    });
    try {
      const started = ok(
        await api.POST("/api/covers/search", {
          body: {
            profile,
            query: { ...query, folder: null },
            providers: names,
            refresh: opts.refresh ?? false,
            review_id: review?.id ?? null,
            album_id: albumId,
          },
        }),
      );
      active.current.add(started.search_id);
      running.current.add(started.search_id);
      for (const e of buffered.current.get(started.search_id) ?? []) apply(e);
      buffered.current.clear();
    } catch (e) {
      setSearchNote(e instanceof Error ? e.message : String(e));
    }
  };

  useEventStream<CoverEvent>(`/api/profiles/${encodeURIComponent(profile)}/events`, {
    types: STREAM_TYPES,
    onOpen: () => {
      // Album mode searches at once (COVER-PICKER-2), once the stream sees its events.
      if (albumId === null || autoStarted.current) return;
      autoStarted.current = true;
      void startSearch(
        rows.filter((p) => p.enabled).map((p) => p.name),
        { fresh: true },
      );
    },
    onEvent: (type, event) => {
      const e = { ...event, type };
      if (type.startsWith("covers.")) {
        const id = e.data.search_id;
        if (id === undefined) return;
        if (active.current.has(id)) apply(e);
        else if (buffered.current.size < 20) {
          // The POST that started it may not have answered yet (COVER-PICKER-5).
          buffered.current.set(id, [...(buffered.current.get(id) ?? []), e]);
        }
        return;
      }
      choiceEvent(e);
    },
  });

  // Leaving the picker cancels its search (COVER-PICKER-5).
  useEffect(() => cancelRunning, []);

  // -- choosing (COVER-PICKER-13, -14) -------------------------------------------------------
  const [outcome, setOutcome] = useState<string | null>(null);
  const [refused, setRefused] = useState<Record<string, string>>({});
  const pendingJob = useRef<string | null>(null);
  const pendingChange = useRef<string | null>(null);
  const choose = useMutation({
    mutationFn: async (c: RankedCandidate) => {
      if (review !== null) {
        return ok(
          await api.POST("/api/covers/reviews/{review_id}/decision", {
            params: { path: { review_id: review.id } },
            body: { decision: "choose", candidate_id: c.id },
          }),
        ).job_id;
      }
      const queued = ok(
        await api.POST("/api/profiles/{profile}/albums/{album_id}/cover", {
          params: { path: { profile, album_id: albumId ?? "" } },
          body: { candidate_id: c.id, old_sha256: currentSha ?? null },
        }),
      );
      pendingChange.current = queued.changeset_id;
      return queued.job_id;
    },
    onMutate: () => {
      setOutcome(null);
    },
    onSuccess: (jobId) => {
      pendingJob.current = jobId ?? null;
      setOutcome("Setting the cover.");
      settle();
      if (review !== null) void queryClient.invalidateQueries({ queryKey: ["review"] });
    },
    onError: (e, c) => {
      setRefused((all) => ({ ...all, [c.id]: e.message }));
    },
  });
  const decide = useMutation({
    mutationFn: async (decision: "skip" | "fail") =>
      ok(
        await api.POST("/api/covers/reviews/{review_id}/decision", {
          params: { path: { review_id: review?.id ?? "" } },
          body: { decision },
        }),
      ),
    onSuccess: () => {
      setOutcome("Done.");
      void queryClient.invalidateQueries({ queryKey: ["review"] });
    },
  });

  // Outcomes seen on the stream, kept because the job can finish before the POST that
  // queued it has answered.
  const changeOutcomes = useRef(new Map<string, string>());
  const finishedJobs = useRef(new Map<string, string>());
  function choiceEvent(e: CoverEvent) {
    if (albumId === null) return;
    if (e.type.startsWith("changeset.") && e.data.changeset_id)
      changeOutcomes.current.set(e.data.changeset_id, e.type);
    if (e.type === "job.finished" && e.data.job_id)
      finishedJobs.current.set(e.data.job_id, e.data.state ?? "");
    settle();
  }
  function settle() {
    const change = pendingChange.current;
    const job = pendingJob.current;
    if (albumId === null || change === null || job === null) return;
    const seen = changeOutcomes.current.get(change);
    if (seen === "changeset.applied") setOutcome("Cover set. Undo it from Recent edits.");
    else if (seen === "changeset.conflicted")
      setOutcome("The cover changed meanwhile; nothing was set. Search again to see it.");
    else if (seen === "changeset.failed") setOutcome("Setting the cover failed.");
    const state = finishedJobs.current.get(job);
    if (state === undefined) return;
    pendingJob.current = null;
    if (state !== "succeeded") setOutcome("Setting the cover failed.");
    void queryClient.invalidateQueries({ queryKey: ["library", profile] });
    void queryClient.invalidateQueries({ queryKey: ["recent-edits", profile] });
    void queryClient.invalidateQueries({ queryKey: ["cover-query", profile, albumId] });
  }

  // -- own images (COVER-PICKER-17 to -19) ---------------------------------------------------
  const [adding, setAdding] = useState(false);
  const addImages = async (blobs: Blob[]) => {
    setMessage(null);
    setAdding(true);
    try {
      for (const blob of blobs) {
        try {
          const added = ok(
            await api.POST("/api/covers/uploads", {
              params: { query: { profile } },
              body: { data: await toBase64(blob) },
            }),
          );
          upsert([added.candidate]);
          if (added.candidate.filtered != null) setShowHidden(true);
        } catch (e) {
          setMessage(e instanceof Error ? e.message : String(e));
        }
      }
    } finally {
      setAdding(false);
    }
  };
  const onTransfer = (data: DataTransfer | null): boolean => {
    const { images, text } = imagesFrom(data);
    if (images.length > 0) {
      void addImages(images);
      return true;
    }
    if (text) {
      setMessage(PASTE_LINK_MESSAGE);
      return true;
    }
    return false;
  };
  const pasteButton = async () => {
    setMessage(null);
    try {
      const items = await navigator.clipboard.read();
      const blobs: Blob[] = [];
      let text = false;
      for (const item of items) {
        const type = IMAGE_TYPES.find((t) => item.types.includes(t));
        if (type !== undefined) blobs.push(await item.getType(type));
        else if (item.types.some((t) => t.startsWith("text/"))) text = true;
      }
      if (blobs.length > 0) await addImages(blobs);
      else setMessage(text ? PASTE_LINK_MESSAGE : "The clipboard holds no image.");
    } catch {
      setMessage("The browser didn't allow reading the clipboard; press Ctrl+V (⌘V) instead.");
    }
  };

  // -- saving defaults (COVER-PICKER-10) -----------------------------------------------------
  const save = useMutation({
    mutationFn: async () =>
      ok(
        await api.PUT("/api/profiles/{profile}/covers/providers", {
          params: { path: { profile } },
          body: { providers: rows, profile_hash: hash },
        }),
      ),
    onSuccess: (saved) => {
      setHash(saved.profile_hash);
      void queryClient.invalidateQueries({ queryKey: ["cover-providers", profile] });
    },
  });

  const setRow = (name: string, change: Partial<ProviderRow>) => {
    setProviders((all) => all.map((p) => (p.name === name ? { ...p, ...change } : p)));
    if (change.enabled === true && !asked.current.has(name) && asked.current.size > 0) {
      void startSearch([name], { fresh: false });
    }
  };
  const move = (i: number, by: number) => {
    setProviders((all) => {
      const next = [...all];
      const j = i + by;
      if (j < 0 || j >= next.length) return next;
      const a = next[i];
      const b = next[j];
      if (a === undefined || b === undefined) return next;
      next[i] = b;
      next[j] = a;
      return next;
    });
  };

  // -- preview (COVER-PICKER-12) -------------------------------------------------------------
  const [preview, setPreview] = useState<RankedCandidate | null>(null);

  const ranked = rankCandidates(candidates.values(), rows, showHidden);
  const hiddenCount = rankCandidates(candidates.values(), rows, true).length - ranked.length;
  const busy = choose.isPending || choose.isSuccess || decide.isPending || decide.isSuccess;
  const enabledNames = rows.filter((p) => p.enabled).map((p) => p.name);
  const label = review?.label ?? (mode.kind === "album" ? mode.label : "");

  return (
    <Stack
      gap="sm"
      data-testid="cover-picker"
      tabIndex={-1}
      onPaste={(e) => {
        if (onTransfer(e.clipboardData)) e.preventDefault();
      }}
      onDragOver={(e) => {
        e.preventDefault();
      }}
      onDrop={(e) => {
        if (onTransfer(e.dataTransfer)) e.preventDefault();
      }}
    >
      <Text size="sm">
        <b>{label}</b>
        {review?.reason ? ` — ${review.reason}` : ""}
      </Text>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void startSearch(enabledNames, { fresh: true });
        }}
      >
        <Group align="end" gap="xs" wrap="wrap">
          <TextInput
            label="Artist"
            size="xs"
            value={query.artist}
            onChange={(e) => {
              const value = e.currentTarget.value;
              setQuery((q) => ({ ...q, artist: value }));
            }}
          />
          <TextInput
            label="Album"
            size="xs"
            value={query.album}
            onChange={(e) => {
              const value = e.currentTarget.value;
              setQuery((q) => ({ ...q, album: value }));
            }}
          />
          <NumberInput
            label="Year"
            size="xs"
            w={90}
            min={0}
            allowDecimal={false}
            value={query.year ?? ""}
            onChange={(v) => {
              setQuery((q) => ({ ...q, year: typeof v === "number" ? v : null }));
            }}
          />
          <TextInput
            label="Catalog number"
            size="xs"
            value={query.catalognum ?? ""}
            onChange={(e) => {
              const value = e.currentTarget.value || null;
              setQuery((q) => ({ ...q, catalognum: value }));
            }}
          />
          <TextInput
            label="Barcode"
            size="xs"
            value={query.barcode ?? ""}
            onChange={(e) => {
              const value = e.currentTarget.value || null;
              setQuery((q) => ({ ...q, barcode: value }));
            }}
          />
          <Button type="submit" size="xs" disabled={enabledNames.length === 0}>
            Search
          </Button>
          <Button
            size="xs"
            variant="default"
            disabled={enabledNames.length === 0}
            onClick={() => {
              void startSearch(enabledNames, { fresh: true, refresh: true });
            }}
          >
            Search again
          </Button>
        </Group>
        {(query.mb_albumid || query.mb_releasegroupid) && (
          <Group gap="xs" mt={4}>
            {query.mb_albumid && (
              <Badge
                variant="light"
                rightSection={
                  <ActionIcon
                    size="xs"
                    variant="transparent"
                    aria-label="Clear the MusicBrainz release id"
                    onClick={() => {
                      setQuery((q) => ({ ...q, mb_albumid: null }));
                    }}
                  >
                    ×
                  </ActionIcon>
                }
              >
                release {query.mb_albumid}
              </Badge>
            )}
            {query.mb_releasegroupid && (
              <Badge
                variant="light"
                rightSection={
                  <ActionIcon
                    size="xs"
                    variant="transparent"
                    aria-label="Clear the MusicBrainz release group id"
                    onClick={() => {
                      setQuery((q) => ({ ...q, mb_releasegroupid: null }));
                    }}
                  >
                    ×
                  </ActionIcon>
                }
              >
                release group {query.mb_releasegroupid}
              </Badge>
            )}
          </Group>
        )}
      </form>

      <ProviderPanel
        rows={rows}
        statuses={statuses}
        dropped={dropped}
        onChange={setRow}
        onMove={move}
        onSave={() => {
          save.mutate();
        }}
        saving={save.isPending}
        saved={save.isSuccess}
        saveError={
          save.error instanceof ApiError && save.error.code === "conflict"
            ? "profile.yaml changed since the picker loaded it. Reload the page to see it."
            : (save.error?.message ?? null)
        }
      />

      <Group gap="xs">
        <FileButton
          accept={IMAGE_TYPES.join(",")}
          multiple
          onChange={(files) => {
            void addImages(files);
          }}
        >
          {(props) => (
            <Button size="xs" variant="default" loading={adding} {...props}>
              Upload image
            </Button>
          )}
        </FileButton>
        <Button
          size="xs"
          variant="default"
          onClick={() => {
            void pasteButton();
          }}
        >
          Paste image
        </Button>
        <Text size="xs" c="dimmed">
          or drop an image here, or press Ctrl+V (⌘V)
        </Text>
        {hiddenCount > 0 || showHidden ? (
          <Checkbox
            size="xs"
            label={`Show hidden (${String(hiddenCount)})`}
            checked={showHidden}
            onChange={(e) => {
              setShowHidden(e.currentTarget.checked);
            }}
          />
        ) : null}
      </Group>
      {message && (
        <Alert color="yellow" data-testid="picker-message">
          {message}
        </Alert>
      )}
      {searchNote && (
        <Text size="sm" c="dimmed" data-testid="search-note">
          {searchNote}
        </Text>
      )}

      <SimpleGrid cols={{ base: 2, sm: 3, md: 5 }} data-testid="cover-grid">
        {currentSha && (
          <Card withBorder padding="xs" data-testid="current-art">
            <Card.Section>
              <Image
                src={
                  review !== null
                    ? candidateImage(profile, currentSha, false)
                    : albumArt(profile, albumId ?? "")
                }
                alt="Current cover"
                h={140}
                fit="contain"
              />
            </Card.Section>
            <Badge size="xs" mt="xs" color="gray">
              Current
            </Badge>
          </Card>
        )}
        {ranked.map(({ candidate: c, score }) => (
          <Card
            key={c.id}
            withBorder
            padding="xs"
            data-testid="cover-candidate"
            data-provider={c.provider}
            data-filtered={c.filtered ?? undefined}
          >
            <Card.Section>
              <UnstyledButton
                w="100%"
                aria-label={`Preview ${c.provider} cover, ${sizeLabel(c)}`}
                onClick={() => {
                  setPreview(c);
                }}
              >
                <Image
                  src={candidateImage(profile, c.id)}
                  alt={`${c.provider} cover, ${sizeLabel(c)}`}
                  h={140}
                  fit="contain"
                />
              </UnstyledButton>
            </Card.Section>
            <Group gap={4} mt="xs">
              <Badge size="xs" variant="light">
                {c.provider}
              </Badge>
              <Text size="xs">{sizeLabel(c)}</Text>
              <Text size="xs" c="dimmed" data-testid="score">
                score {score.toFixed(2)}
              </Text>
            </Group>
            {c.filtered != null && (
              <Text size="xs" c="orange">
                hidden: {FILTER_LABEL[c.filtered] ?? c.filtered}
              </Text>
            )}
            {c.title && (
              <Text size="xs" c="dimmed" lineClamp={1}>
                {c.title}
              </Text>
            )}
            {refused[c.id] && (
              <Text size="xs" c="red" data-testid="refused">
                {refused[c.id]}
              </Text>
            )}
            <Button
              size="xs"
              mt="xs"
              disabled={busy}
              onClick={() => {
                choose.mutate(c);
              }}
            >
              Use this cover
            </Button>
          </Card>
        ))}
      </SimpleGrid>
      {ranked.length === 0 && (
        <Text size="sm" c="dimmed" data-testid="no-candidates">
          {Object.values(statuses).some((s) => s.state === "searching")
            ? "Searching…"
            : "No cover found yet. Edit the query and search, or add an image of your own."}
        </Text>
      )}

      {review !== null && (
        <Group>
          <Button
            variant="default"
            size="xs"
            disabled={busy}
            onClick={() => {
              decide.mutate("skip");
            }}
          >
            No cover
          </Button>
          <Button
            variant="default"
            color="red"
            size="xs"
            disabled={busy}
            onClick={() => {
              decide.mutate("fail");
            }}
          >
            Fail this album
          </Button>
        </Group>
      )}
      {decide.error && <Alert color="red">{decide.error.message}</Alert>}
      {outcome && (
        <Text size="sm" c="dimmed" data-testid="decision-sent">
          {outcome}
        </Text>
      )}

      <CoverPreview
        key={preview?.id ?? "none"}
        profile={profile}
        candidate={preview}
        disabled={busy}
        refused={preview ? refused[preview.id] : undefined}
        onClose={() => {
          setPreview(null);
        }}
        onChoose={(c) => {
          setPreview(null);
          choose.mutate(c);
        }}
      />
    </Stack>
  );
}

const STATE_LABEL: Record<ProviderStatus["state"], string> = {
  searching: "searching",
  done: "done",
  failed: "failed",
  skipped: "skipped",
};

function statusText(s: ProviderStatus): string {
  if (s.state === "done")
    return s.cached ? `cached, ${String(s.count)}` : `done, ${String(s.count)}`;
  if (s.state === "failed") return `failed${s.message ? `: ${s.message}` : ""}`;
  if (s.state === "skipped") return `skipped${s.message ? `: ${s.message}` : ""}`;
  return STATE_LABEL[s.state];
}

/** Per-provider toggles, weights, order and status (COVER-PICKER-7, -9 to -11). */
function ProviderPanel(props: {
  rows: ProviderRow[];
  statuses: Record<string, ProviderStatus>;
  dropped: Record<string, Record<string, number>>;
  onChange: (name: string, change: Partial<ProviderRow>) => void;
  onMove: (index: number, by: number) => void;
  onSave: () => void;
  saving: boolean;
  saved: boolean;
  saveError: string | null;
}) {
  return (
    <Stack gap={4} data-testid="provider-panel">
      {props.rows.map((p, i) => {
        const status = props.statuses[p.name];
        const dropped = props.dropped[p.name] ?? {};
        const hidden = Object.entries(dropped)
          .map(([reason, n]) => `${String(n)} ${FILTER_LABEL[reason] ?? reason}`)
          .join(", ");
        return (
          <Group key={p.name} gap="xs" wrap="nowrap" data-testid="provider-row" data-name={p.name}>
            <Switch
              size="xs"
              aria-label={`Use ${p.name}`}
              checked={p.enabled}
              onChange={(e) => {
                props.onChange(p.name, { enabled: e.currentTarget.checked });
              }}
            />
            <Text size="sm" w={130} truncate>
              {p.name}
            </Text>
            <Slider
              w={160}
              min={0}
              max={10}
              step={0.1}
              label={(v) => v.toFixed(1)}
              value={p.weight}
              disabled={!p.enabled}
              aria-label={`${p.name} weight`}
              onChange={(v) => {
                props.onChange(p.name, { weight: v });
              }}
            />
            <Text size="xs" w={30} data-testid="weight">
              {p.weight.toFixed(1)}
            </Text>
            <ActionIcon
              size="sm"
              variant="subtle"
              aria-label={`Move ${p.name} up`}
              disabled={i === 0}
              onClick={() => {
                props.onMove(i, -1);
              }}
            >
              ↑
            </ActionIcon>
            <ActionIcon
              size="sm"
              variant="subtle"
              aria-label={`Move ${p.name} down`}
              disabled={i === props.rows.length - 1}
              onClick={() => {
                props.onMove(i, 1);
              }}
            >
              ↓
            </ActionIcon>
            {status && (
              <Badge
                size="xs"
                variant="light"
                color={
                  status.state === "failed"
                    ? "red"
                    : status.state === "searching"
                      ? "blue"
                      : status.state === "skipped"
                        ? "gray"
                        : "green"
                }
                data-testid="provider-status"
                title={status.message ?? undefined}
              >
                {statusText(status)}
              </Badge>
            )}
            {hidden && (
              <Text size="xs" c="dimmed">
                {hidden} hidden
              </Text>
            )}
          </Group>
        );
      })}
      <Group gap="xs">
        <Button
          size="xs"
          variant="default"
          loading={props.saving}
          disabled={props.rows.length === 0}
          onClick={props.onSave}
        >
          Save as defaults
        </Button>
        {props.saved && (
          <Text size="xs" c="dimmed" data-testid="saved">
            Saved to profile.yaml.
          </Text>
        )}
        {props.saveError && (
          <Text size="xs" c="red">
            {props.saveError}
          </Text>
        )}
      </Group>
    </Stack>
  );
}

/** The full-size image with its real dimensions and file size (COVER-PICKER-12). */
function CoverPreview(props: {
  profile: string;
  candidate: RankedCandidate | null;
  disabled: boolean;
  refused: string | undefined;
  onClose: () => void;
  onChoose: (c: RankedCandidate) => void;
}) {
  const c = props.candidate;
  const [src, setSrc] = useState<string | null>(null);
  const [bytes, setBytes] = useState<number | null>(null);
  const [dims, setDims] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const id = c?.id ?? null;
  const profile = props.profile;
  useEffect(() => {
    if (id === null) return;
    let url: string | null = null;
    const life = { gone: false };
    void (async () => {
      try {
        const r = await fetch(new URL(candidateImage(profile, id, false), window.location.origin), {
          credentials: "same-origin",
        });
        if (!r.ok) {
          const body = (await r.json().catch(() => null)) as {
            error?: { message?: string };
          } | null;
          throw new Error(
            body?.error?.message ?? `the image couldn't be loaded (${String(r.status)})`,
          );
        }
        const blob = await r.blob();
        if (life.gone) return;
        url = URL.createObjectURL(blob);
        setBytes(blob.size);
        setSrc(url);
      } catch (e) {
        if (!life.gone) setFailed(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      life.gone = true;
      if (url !== null) URL.revokeObjectURL(url);
    };
  }, [id, profile]);

  return (
    <Modal opened={c !== null} onClose={props.onClose} title="Cover preview" size="xl">
      {c && (
        <Stack gap="xs" data-testid="cover-preview">
          {src ? (
            <Image
              src={src}
              alt={`${c.provider} cover, full size`}
              fit="contain"
              mah="70vh"
              onLoad={(e) => {
                const img = e.currentTarget;
                setDims(`${String(img.naturalWidth)}×${String(img.naturalHeight)}`);
              }}
            />
          ) : failed ? (
            <Alert color="red" data-testid="preview-failed">
              {failed}
            </Alert>
          ) : (
            <Text size="sm" c="dimmed">
              Loading…
            </Text>
          )}
          <Group gap="xs">
            <Badge variant="light">{c.provider}</Badge>
            <Text size="sm" data-testid="preview-size">
              {dims ?? sizeLabel(c)}
              {bytes !== null ? `, ${bytesLabel(bytes)}` : ""}
            </Text>
            {c.title && <Text size="sm">{c.title}</Text>}
            {c.source_url && (
              <Anchor href={c.source_url} target="_blank" rel="noopener noreferrer" size="sm">
                source page
              </Anchor>
            )}
          </Group>
          {props.refused && (
            <Text size="sm" c="red">
              {props.refused}
            </Text>
          )}
          <Group>
            <Button
              data-autofocus
              disabled={props.disabled || failed !== null}
              onClick={() => {
                props.onChoose(c);
              }}
            >
              Use this cover
            </Button>
            <Button variant="default" onClick={props.onClose}>
              Close
            </Button>
          </Group>
        </Stack>
      )}
    </Modal>
  );
}
