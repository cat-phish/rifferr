import {
  Badge,
  Button,
  Group,
  Image,
  Modal,
  Popover,
  Stack,
  Switch,
  Table,
  Text,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, ok } from "../api/client";
import type { ChangeSet } from "../api/types";
import { albumArt } from "../lib/covers";
import {
  albumRow,
  display,
  itemRow,
  listFields,
  show,
  withEdit,
  type AlbumSnapshot,
  type Edit,
  type FieldValue,
  type Pending,
  type Row,
} from "../lib/grid";
import { CoverPicker } from "./CoverPicker";
import { CellEditor } from "./TagGrid";

type Before = Record<string, FieldValue>;
type FolderItem = { tags?: Record<string, FieldValue> };

const same = (a: FieldValue | undefined, b: FieldValue | undefined) =>
  JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

/** Fields to show: the step's `show` list, then every field the run changed (LIBRARY-MOVE-18).
 * Nothing here names a tag: without `show`, only changed fields and the files' imported tags. */
function fieldList(listed: string[] | null | undefined, changed: Iterable<string>): string[] {
  const out = [...(listed ?? [])];
  for (const f of changed) if (!out.includes(f)) out.push(f);
  return out;
}

/** The file's tags before the import, matched by disc and track, else by position. */
function folderTags(
  folder: FolderItem[],
  fields: Record<string, FieldValue>,
  index: number,
): Before {
  const key = (t: Record<string, FieldValue>) => `${show(t.disc ?? 1)}/${show(t.track)}`;
  const want = key(fields);
  const match = folder.find((f) => f.tags?.track != null && key(f.tags) === want);
  return (match ?? folder[index])?.tags ?? {};
}

function Value({
  now,
  before,
  known,
  kind,
}: {
  now: FieldValue | undefined;
  before: FieldValue | undefined;
  known: boolean;
  /** TAG-GRID-16: a duration shows as m:ss. */
  kind?: "duration";
}) {
  const text = display(now, kind);
  if (!known || same(now, before))
    return (
      <Text span size="sm" c={text === "" ? "dimmed" : undefined}>
        {text === "" ? "—" : text}
      </Text>
    );
  const was = display(before, kind);
  return (
    <Tooltip label={`Before this run: ${was === "" ? "(empty)" : was}`} withArrow>
      <Group gap={4} wrap="nowrap" data-changed="true" style={{ display: "inline-flex" }}>
        <Text span size="sm" fw={700}>
          {text === "" ? "(empty)" : text}
        </Text>
        <Badge size="xs" variant="light" color="yellow">
          changed
        </Badge>
      </Group>
    </Tooltip>
  );
}

/** A value that opens the tag grid's editor on click (LIBRARY-MOVE-18, final review). A
 * pending edit shows as old → new until Apply; the before-run tooltip stays. */
/** A small pencil marking a value as editable (REVIEW-14). Decorative: the button
 * around it carries the label. */
function Pencil() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      data-testid="edit-pencil"
      style={{ opacity: 0.55, flexShrink: 0, color: "var(--mantine-color-dimmed)" }}
    >
      <path d="M4 20h4L18.5 9.5a2.1 2.1 0 0 0-3-3L5 17v3z" />
      <path d="M13.5 6.5l3 3" />
    </svg>
  );
}

function EditableValue({
  row,
  field,
  pending,
  lists,
  onEdit,
  readOnly,
  children,
}: {
  row: Row;
  field: string;
  pending: Pending;
  lists: Set<string>;
  onEdit: (row: Row, field: string, edit: Edit | null) => void;
  readOnly: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  if (readOnly) return <>{children}</>;
  const edit = pending[row.key]?.changes[field];
  return (
    <Popover opened={open} onChange={setOpen} position="bottom-start" withinPortal trapFocus>
      <Popover.Target>
        <UnstyledButton
          data-testid="check-value"
          data-field={field}
          data-pending={edit ? "true" : undefined}
          aria-label={`Edit ${field}`}
          onClick={() => {
            setOpen(true);
          }}
          style={{
            cursor: "pointer",
            borderRadius: 4,
            padding: "0 4px",
            background: edit ? "var(--mantine-color-yellow-light)" : undefined,
          }}
        >
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            {edit ? (
              <Text span size="sm">
                <s style={{ opacity: 0.6 }}>{show(edit.old) || "(none)"}</s> →{" "}
                <b>{edit.op === "delete" ? "(deleted)" : show(edit.new) || "(empty)"}</b>
              </Text>
            ) : (
              children
            )}
            <Pencil />
          </span>
        </UnstyledButton>
      </Popover.Target>
      <Popover.Dropdown>
        {open && (
          <CellEditor
            row={row}
            field={field}
            edit={edit}
            problem={undefined}
            isList={Array.isArray(row.fields[field]) || lists.has(field)}
            lists={lists}
            onDone={(next) => {
              onEdit(row, field, next);
              setOpen(false);
            }}
            onCancel={() => {
              setOpen(false);
            }}
          />
        )}
      </Popover.Dropdown>
    </Popover>
  );
}

const ART = "art_sha256";

function heldCover(e: Edit | undefined): string | null {
  return e?.op === "set" && typeof e.new === "string" ? e.new : null;
}

function Cover({
  profile,
  albumId,
  label,
  heldSha,
  onHeld,
}: {
  profile: string;
  albumId: string;
  label: string;
  /** A cover chosen at an `edit: true` gate, not written yet (COVER-PICKER-22). */
  heldSha?: string | null;
  onHeld?: (sha256: string, old: string | null) => void;
}) {
  const [size, setSize] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [full, setFull] = useState(false);
  const [picker, setPicker] = useState(false);
  const src = heldSha
    ? `/api/profiles/${encodeURIComponent(profile)}/art/${heldSha}`
    : albumArt(profile, albumId);
  return (
    <Stack gap={4} align="flex-start" data-testid="album-check-cover">
      {missing ? (
        <Text size="sm" c="dimmed" w={300}>
          No cover
        </Text>
      ) : (
        <UnstyledButton
          aria-label="Show the cover full size"
          onClick={() => {
            setFull(true);
          }}
        >
          <Image
            src={src}
            alt={`Cover of ${label}`}
            w={300}
            h={300}
            fit="contain"
            radius="sm"
            onLoad={(e) => {
              const img = e.currentTarget;
              setSize(`${String(img.naturalWidth)}×${String(img.naturalHeight)}`);
            }}
            onError={() => {
              setMissing(true);
            }}
          />
        </UnstyledButton>
      )}
      <Group gap="xs">
        {size !== null && (
          <Text size="sm" data-testid="cover-size">
            {size} px
          </Text>
        )}
        {heldSha && (
          <Badge size="xs" variant="light" color="yellow" data-testid="cover-held">
            new, held
          </Badge>
        )}
        <Button
          size="compact-xs"
          variant="light"
          onClick={() => {
            setPicker(true);
          }}
        >
          Change cover
        </Button>
      </Group>
      <Modal
        opened={full}
        onClose={() => {
          setFull(false);
        }}
        title={size ?? "Cover"}
        size="auto"
      >
        <Image src={src} alt={`Cover of ${label}, full size`} maw="90vw" mah="85vh" fit="contain" />
      </Modal>
      <Modal
        opened={picker}
        onClose={() => {
          setPicker(false);
          setMissing(false);
          setSize(null);
        }}
        title="Change cover"
        size="90%"
      >
        {picker && (
          <CoverPicker
            profile={profile}
            mode={{
              kind: "album",
              albumId,
              label,
              ...(onHeld
                ? {
                    onHeld: (sha: string, old: string | null) => {
                      onHeld(sha, old);
                      setPicker(false);
                      setMissing(false);
                      setSize(null);
                    },
                  }
                : {}),
            }}
          />
        )}
      </Modal>
    </Stack>
  );
}

/** The album's final check above a FileChangeSet (LIBRARY-MOVE-18): its cover with its size,
 * and the `show` fields of the album and its tracks, each field this run changed marked. */
export function AlbumCheck({ changeset }: { changeset: ChangeSet }) {
  if (!changeset.album) return null;
  return (
    <AlbumCheckView
      profile={changeset.profile}
      albumId={changeset.album.id}
      source={{ kind: "changeset", id: changeset.id }}
      show={changeset.show ?? undefined}
      readOnly
    />
  );
}

/** Edits held on an `edit: true` review gate instead of applied (S-REVIEW REVIEW-14). */
export interface HeldEdits {
  pending: Pending;
  onChange: (next: Pending) => void;
  saving?: boolean;
}

type BeforeSource = { kind: "changeset"; id: string } | { kind: "run"; runId: string };

/** An album's final check (LIBRARY-MOVE-18, REVIEW-13): cover, fields and tracks, each
 * editable. Without `held`, Apply writes the edits at once as manual ChangeSets; with it,
 * the edits are held on the gate and written when the album is continued. */
export function AlbumCheckView({
  profile,
  albumId,
  source,
  show: shown,
  held,
  readOnly = false,
}: {
  profile: string;
  albumId: string;
  source: BeforeSource;
  show?: { album?: string[] | null; item?: string[] | null };
  held?: HeldEdits;
  /** The move's review: fields are edited at the final-write gate before it (REVIEW-13). */
  readOnly?: boolean;
}) {
  const changeset = { show: shown };
  const [local, setLocal] = useState<Pending>({});
  const pending = held ? held.pending : local;
  const setPending = (next: Pending | ((p: Pending) => Pending)) => {
    const value = typeof next === "function" ? next(pending) : next;
    if (held) held.onChange(value);
    else setLocal(value);
  };
  const [all, setAll] = useState(false);
  const before = useQuery({
    queryKey: ["album-before", source, albumId],
    queryFn: async () =>
      source.kind === "changeset"
        ? ok(
            await api.GET("/api/changesets/{changeset_id}/before", {
              params: { path: { changeset_id: source.id } },
            }),
          )
        : ok(
            await api.GET("/api/runs/{run_id}/albums/{album_id}/before", {
              params: { path: { run_id: source.runId, album_id: albumId } },
            }),
          ),
  });
  const album = useQuery({
    queryKey: ["album-check", profile, albumId],
    retry: false,
    queryFn: async () =>
      ok(
        await api.GET("/api/profiles/{profile}/library", {
          params: { path: { profile }, query: { album: albumId, scope: "album" } },
        }),
      ),
  });
  const snap: AlbumSnapshot | undefined = album.data?.albums[0];
  if (snap === undefined) return null;
  const b = before.data;
  const kinds = album.data?.field_kinds ?? {};
  const items = snap.items ?? [];
  const folder: FolderItem[] = b?.folder ?? [];
  const itemBefore = (id: string, fields: Record<string, FieldValue>, i: number): Before => ({
    ...folderTags(folder, fields, i),
    ...(b?.items?.[id] ?? {}),
  });
  const albumBefore: Before = { ...(folder[0]?.tags ?? {}), ...(b?.album ?? {}) };
  const albumKnown = new Set(Object.keys(b?.album ?? {}));
  for (const k of Object.keys(folder[0]?.tags ?? {})) if (k in snap.fields) albumKnown.add(k);
  const changedAlbum = [...albumKnown].filter((f) => !same(snap.fields[f], albumBefore[f]));
  const albumFields = all
    ? fieldList(fieldList(changeset.show?.album, changedAlbum), Object.keys(snap.fields).sort())
    : fieldList(changeset.show?.album, changedAlbum);
  const changedItem = new Set<string>();
  items.forEach((it, i) => {
    const was = itemBefore(it.id, it.fields, i);
    for (const k of Object.keys(was))
      if (k in it.fields && !same(it.fields[k], was[k]) && !albumFields.includes(k))
        changedItem.add(k);
  });
  const shownItems = fieldList(changeset.show?.item, changedItem);
  const itemFields = all
    ? fieldList(shownItems, [...new Set(items.flatMap((it) => Object.keys(it.fields)))].sort())
    : shownItems;
  const label = `${show(snap.fields.albumartist)} - ${show(snap.fields.album)}`;
  const lists = listFields([snap]);
  const onEdit = (row: Row, field: string, e: Edit | null) => {
    setPending((p) => withEdit(p, row, field, e));
  };
  const count = Object.values(pending).reduce((n, p) => n + Object.keys(p.changes).length, 0);
  const aRow = albumRow(snap);
  return (
    <Group align="flex-start" gap="lg" wrap="wrap" data-testid="album-check">
      <Cover
        profile={profile}
        albumId={albumId}
        label={label}
        heldSha={held ? heldCover(pending[aRow.key]?.changes[ART]) : null}
        onHeld={
          held
            ? (sha, old) => {
                onEdit(aRow, ART, { op: "set", old: aRow.fields[ART] ?? old ?? null, new: sha });
              }
            : undefined
        }
      />
      <Stack gap="sm" style={{ flex: 1, minWidth: 280 }}>
        <Group gap="sm" justify="space-between" data-testid="album-check-edit">
          <Text size="xs" c="dimmed">
            {readOnly ? "" : "Click a value to edit it."}
          </Text>
          <Switch
            size="xs"
            label="All fields"
            checked={all}
            onChange={(e) => {
              setAll(e.currentTarget.checked);
            }}
          />
        </Group>
        {held && count > 0 && (
          <Group gap="xs" data-testid="album-check-held">
            <Text size="sm">
              {count} edit{count === 1 ? "" : "s"} held, written when you continue
              {held.saving ? " (saving…)" : ""}
            </Text>
            <Button
              size="compact-sm"
              variant="default"
              onClick={() => {
                setPending({});
              }}
            >
              Discard edits
            </Button>
          </Group>
        )}
        {albumFields.length > 0 && (
          <Table fz="sm" verticalSpacing={2} withRowBorders={false} data-testid="album-check-album">
            <Table.Tbody>
              {albumFields.map((f) => (
                <Table.Tr key={f} data-field={f}>
                  <Table.Td c="dimmed" ff="monospace" w={180}>
                    {f}
                  </Table.Td>
                  <Table.Td>
                    <EditableValue
                      row={aRow}
                      field={f}
                      pending={pending}
                      lists={lists}
                      onEdit={onEdit}
                      readOnly={readOnly}
                    >
                      <Value
                        now={snap.fields[f]}
                        before={albumBefore[f]}
                        known={f in albumBefore}
                        kind={kinds[f]}
                      />
                    </EditableValue>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
        {itemFields.length > 0 && (
          <Table.ScrollContainer minWidth={400}>
            <Table fz="sm" verticalSpacing={2} striped data-testid="album-check-items">
              <Table.Thead>
                <Table.Tr>
                  {itemFields.map((f) => (
                    <Table.Th key={f} ff="monospace">
                      {f}
                    </Table.Th>
                  ))}
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {items.map((it, i) => {
                  const was = itemBefore(it.id, it.fields, i);
                  return (
                    <Table.Tr key={it.id} data-item={it.id}>
                      {itemFields.map((f) => (
                        <Table.Td key={f} data-field={f}>
                          <EditableValue
                            row={itemRow(snap, it)}
                            field={f}
                            pending={pending}
                            lists={lists}
                            onEdit={onEdit}
                            readOnly={readOnly}
                          >
                            <Value
                              now={it.fields[f]}
                              before={was[f]}
                              known={f in was}
                              kind={kinds[f]}
                            />
                          </EditableValue>
                        </Table.Td>
                      ))}
                    </Table.Tr>
                  );
                })}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
      </Stack>
    </Group>
  );
}
