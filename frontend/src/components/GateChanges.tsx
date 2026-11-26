import { Badge, Group, Table, Text, Tooltip } from "@mantine/core";
import { useMemo, useState } from "react";
import type { GateAlbum } from "../api/types";
import { groupChanges } from "../lib/gateChanges";
import { formatValue } from "../lib/review";
import { Diff } from "./Diff";
import { useFold } from "./Fold";
import type { ChangeRow } from "../lib/gateChanges";

/** Rifferr's album field for the cover: beets' `artpath`, named by the image's SHA-256
 * so a ChangeSet says exactly which image (COVERS-SEARCH-22). */
const ART_FIELD = "art_sha256";

function FieldName({ row }: { row: ChangeRow }) {
  if (row.field === ART_FIELD) {
    return (
      <Tooltip
        multiline
        w={300}
        label="art_sha256: the album's cover (beets artpath), named by the image's SHA-256. It isn't written into the tags."
      >
        <Text span fz="sm" ff="monospace" td="underline dotted">
          cover
        </Text>
      </Tooltip>
    );
  }
  if (row.label !== undefined) {
    return (
      <Tooltip label={`beets fields changed: ${(row.fields ?? []).join(", ")}`}>
        <Text span fz="sm" ff="monospace" td="underline dotted">
          {row.label}
        </Text>
      </Tooltip>
    );
  }
  return <>{row.field}</>;
}

/** The cover before → after as thumbnails from the art store (COVER-PICKER-22). */
function CoverChange({
  profile,
  before,
  after,
}: {
  profile: string;
  before: ChangeRow["before"];
  after: ChangeRow["after"];
}) {
  return (
    <Group gap={8} wrap="nowrap" data-diff="replaced">
      <Thumb profile={profile} sha={before} faded />
      <Text span c="dimmed" aria-label="becomes">
        →
      </Text>
      <Thumb profile={profile} sha={after} />
    </Group>
  );
}

function Thumb({
  profile,
  sha,
  faded = false,
}: {
  profile: string;
  sha: ChangeRow["after"];
  faded?: boolean;
}) {
  const [size, setSize] = useState<string | null>(null);
  const [broken, setBroken] = useState(false);
  if (typeof sha !== "string" || sha === "") {
    return (
      <Text span fz="sm" c="dimmed">
        (none)
      </Text>
    );
  }
  if (broken) {
    return (
      <Text span fz="sm" c="dimmed">
        (image unavailable)
      </Text>
    );
  }
  return (
    <Tooltip label={size ?? sha}>
      <img
        src={`/api/profiles/${encodeURIComponent(profile)}/art/${sha}`}
        alt={faded ? "cover before" : "cover after"}
        width={48}
        height={48}
        loading="lazy"
        style={{ objectFit: "cover", borderRadius: 3, opacity: faded ? 0.6 : 1 }}
        onLoad={(e) => {
          const img = e.currentTarget;
          setSize(`${String(img.naturalWidth)}×${String(img.naturalHeight)} px`);
        }}
        onError={() => {
          setBroken(true);
        }}
      />
    </Tooltip>
  );
}

/** Lined, striped rows for a gate's tables (REVIEW-4): field names line up with values. */
export const GATE_TABLE = {
  fz: "sm",
  striped: true,
  withRowBorders: true,
  verticalSpacing: 6,
  horizontalSpacing: "sm",
} as const;

/** The run's changes to an album so far, grouped: one row per field, value and steps,
 * naming the tracks it touched and the steps that made it (S-REVIEW REVIEW-4). */
export function GateChanges({ album, profile }: { album: GateAlbum; profile: string }) {
  const rows = useMemo(() => groupChanges(album), [album]);
  const fold = useFold(rows);
  if (rows.length === 0) {
    return (
      <Text size="xs" c="dimmed">
        No tag changes by this run so far.
      </Text>
    );
  }
  return (
    <>
      <Table {...GATE_TABLE} data-testid="gate-changes">
        <Table.Thead>
          <Table.Tr>
            <Table.Th w={180}>Changed by this run</Table.Th>
            <Table.Th>Where</Table.Th>
            <Table.Th>Before → after</Table.Th>
            <Table.Th>By step</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {fold.shown.map((r) => (
            <Table.Tr key={r.key} data-testid="gate-change-row">
              <Table.Td ff="monospace" data-testid="gate-change-field">
                <FieldName row={r} />
              </Table.Td>
              <Table.Td data-testid="gate-change-where">
                {r.tracks.length > 1 ? (
                  <Tooltip label={r.tracks.join("\n")} style={{ whiteSpace: "pre-line" }}>
                    <Text span fz="sm" td="underline dotted">
                      {r.where}
                    </Text>
                  </Tooltip>
                ) : (
                  r.where
                )}
              </Table.Td>
              <Table.Td data-testid="gate-change">
                {r.field === ART_FIELD ? (
                  <CoverChange
                    profile={profile}
                    before={r.existedBefore ? r.before : null}
                    after={r.after}
                  />
                ) : (
                  <Diff
                    old={r.existedBefore ? formatValue(r.before) : ""}
                    new={formatValue(r.after)}
                    empty="—"
                  />
                )}
              </Table.Td>
              <Table.Td>
                <Group gap={4}>
                  {r.steps.map((s) => (
                    <Badge
                      key={s}
                      size="sm"
                      variant="light"
                      tt="none"
                      data-testid="gate-change-step"
                    >
                      {s}
                    </Badge>
                  ))}
                </Group>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      {fold.control}
    </>
  );
}
