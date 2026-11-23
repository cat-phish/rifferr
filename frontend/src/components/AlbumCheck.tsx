import {
  Badge,
  Button,
  Group,
  Image,
  Modal,
  Stack,
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
import { show, type AlbumSnapshot, type FieldValue } from "../lib/grid";
import { CoverPicker } from "./CoverPicker";

type Before = Record<string, FieldValue>;
type FolderItem = { tags?: Record<string, FieldValue> };

const same = (a: FieldValue | undefined, b: FieldValue | undefined) =>
  JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

/** Fields to show: the step's `show` list, then every field the run changed (LIBRARY-MOVE-18).
 * Nothing here names a tag: without `show`, only changed fields and the files' imported tags. */
function fieldList(listed: string[] | undefined, changed: Iterable<string>): string[] {
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
}: {
  now: FieldValue | undefined;
  before: FieldValue | undefined;
  known: boolean;
}) {
  const text = show(now);
  if (!known || same(now, before))
    return (
      <Text span size="sm" c={text === "" ? "dimmed" : undefined}>
        {text === "" ? "—" : text}
      </Text>
    );
  const was = show(before);
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

function Cover({ profile, albumId, label }: { profile: string; albumId: string; label: string }) {
  const [size, setSize] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [full, setFull] = useState(false);
  const [picker, setPicker] = useState(false);
  const src = albumArt(profile, albumId);
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
        {picker && <CoverPicker profile={profile} mode={{ kind: "album", albumId, label }} />}
      </Modal>
    </Stack>
  );
}

/** The album's final check above a FileChangeSet (LIBRARY-MOVE-18): its cover with its size,
 * and the `show` fields of the album and its tracks, each field this run changed marked. */
export function AlbumCheck({ changeset }: { changeset: ChangeSet }) {
  const profile = changeset.profile;
  const albumId = changeset.album?.id ?? null;
  const before = useQuery({
    queryKey: ["changeset-before", changeset.id],
    queryFn: async () =>
      ok(
        await api.GET("/api/changesets/{changeset_id}/before", {
          params: { path: { changeset_id: changeset.id } },
        }),
      ),
  });
  const album = useQuery({
    queryKey: ["album-check", profile, albumId],
    enabled: albumId !== null,
    retry: false,
    queryFn: async () =>
      ok(
        await api.GET("/api/profiles/{profile}/library", {
          params: { path: { profile }, query: { album: albumId ?? "", scope: "album" } },
        }),
      ),
  });
  const snap: AlbumSnapshot | undefined = album.data?.albums[0];
  if (albumId === null || snap === undefined) return null;
  const b = before.data;
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
  const albumFields = fieldList(changeset.show?.album, changedAlbum);
  const changedItem = new Set<string>();
  items.forEach((it, i) => {
    const was = itemBefore(it.id, it.fields, i);
    for (const k of Object.keys(was))
      if (k in it.fields && !same(it.fields[k], was[k]) && !albumFields.includes(k))
        changedItem.add(k);
  });
  const itemFields = fieldList(changeset.show?.item, changedItem);
  const label = `${show(snap.fields.albumartist)} - ${show(snap.fields.album)}`;
  return (
    <Group align="flex-start" gap="lg" wrap="wrap" data-testid="album-check">
      <Cover profile={profile} albumId={albumId} label={label} />
      <Stack gap="sm" style={{ flex: 1, minWidth: 280 }}>
        {albumFields.length > 0 && (
          <Table fz="sm" verticalSpacing={2} withRowBorders={false} data-testid="album-check-album">
            <Table.Tbody>
              {albumFields.map((f) => (
                <Table.Tr key={f} data-field={f}>
                  <Table.Td c="dimmed" ff="monospace" w={180}>
                    {f}
                  </Table.Td>
                  <Table.Td>
                    <Value now={snap.fields[f]} before={albumBefore[f]} known={f in albumBefore} />
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
                          <Value now={it.fields[f]} before={was[f]} known={f in was} />
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
