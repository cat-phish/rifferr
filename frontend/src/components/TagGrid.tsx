import {
  ActionIcon,
  Box,
  Button,
  Checkbox,
  Group,
  Popover,
  Stack,
  TagsInput,
  Text,
  TextInput,
  Tooltip,
} from "@mantine/core";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useRef, useState } from "react";
import {
  type AlbumSnapshot,
  type Edit,
  type FieldValue,
  MIN_WIDTH,
  type Pending,
  type Row,
  type Scope,
  albumRow,
  cellKey,
  deleteEdit,
  itemRow,
  setEdit,
  show,
} from "../lib/grid";

const ROW = 34;
const SELECT = 36;
const EXPAND = 32;

/** A conflict from apply: what the field holds now and what the user wanted (TAG-GRID-10). */
export interface CellConflict {
  current: FieldValue;
  wanted: Edit;
}

export interface GridProps {
  albums: AlbumSnapshot[];
  scope: Scope;
  columns: Record<Scope, string[]>;
  widths: Record<Scope, Record<string, number>>;
  defaultWidth: number;
  onResize: (scope: Scope, field: string, width: number) => void;
  expanded: Set<string>;
  onToggle: (albumId: string) => void;
  selected: Set<string>;
  onSelect: (rows: Row[], on: boolean) => void;
  pending: Pending;
  problems: Map<string, string>;
  conflicts: Map<string, CellConflict>;
  failures: Map<string, string>;
  busy: Set<string>;
  lists: Set<string>;
  onEdit: (row: Row, field: string, edit: Edit | null) => void;
  height?: number;
}

type Line =
  | { kind: "album"; row: Row; album: AlbumSnapshot }
  | { kind: "items"; album: AlbumSnapshot }
  | { kind: "item"; row: Row };

function lines(albums: AlbumSnapshot[], scope: Scope, expanded: Set<string>): Line[] {
  const out: Line[] = [];
  for (const a of albums) {
    if (scope === "item") {
      for (const i of a.items ?? []) out.push({ kind: "item", row: itemRow(a, i) });
      continue;
    }
    out.push({ kind: "album", row: albumRow(a), album: a });
    if (expanded.has(a.id)) {
      out.push({ kind: "items", album: a });
      for (const i of a.items ?? []) out.push({ kind: "item", row: itemRow(a, i) });
    }
  }
  return out;
}

/**
 * The library as a virtualized grid (TAG-GRID-1, -4, -5): albums expand to their items,
 * cells edit in place into pending edits. Values are always text, never HTML (TAG-GRID-14).
 */
export function TagGrid(props: GridProps) {
  const { albums, scope, expanded, height = 520 } = props;
  const parent = useRef<HTMLDivElement>(null);
  const all = lines(albums, scope, expanded);
  // eslint-disable-next-line react-hooks/incompatible-library -- the virtualizer is meant to be used this way
  const virtualizer = useVirtualizer({
    count: all.length,
    getScrollElement: () => parent.current,
    estimateSize: () => ROW,
    overscan: 10,
  });
  const width = (s: Scope) =>
    SELECT +
    EXPAND +
    props.columns[s].reduce((sum, c) => sum + (props.widths[s][c] ?? props.defaultWidth), 0);
  const rows = all.flatMap((l) => (l.kind === "items" ? [] : [l.row]));
  const allSelected = rows.length > 0 && rows.every((r) => props.selected.has(r.key));

  return (
    <Box
      ref={parent}
      role="grid"
      aria-label="Library"
      aria-rowcount={all.length}
      data-testid="tag-grid"
      style={{
        height,
        overflow: "auto",
        border: "1px solid var(--mantine-color-default-border)",
        borderRadius: 4,
      }}
    >
      <div
        role="row"
        style={{
          display: "flex",
          position: "sticky",
          top: 0,
          zIndex: 2,
          minWidth: width(scope),
          background: "var(--mantine-color-body)",
          borderBottom: "1px solid var(--mantine-color-default-border)",
          fontWeight: 600,
          fontSize: 13,
        }}
      >
        <Cellbox w={SELECT}>
          <Checkbox
            size="xs"
            aria-label="Select all"
            checked={allSelected}
            onChange={(e) => {
              props.onSelect(rows, e.currentTarget.checked);
            }}
          />
        </Cellbox>
        <Cellbox w={EXPAND} />
        {props.columns[scope].map((c) => (
          <HeaderCell key={c} {...props} scope={scope} field={c} />
        ))}
      </div>
      <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
        {virtualizer.getVirtualItems().map((v) => {
          const line = all[v.index];
          if (line === undefined) return null;
          const style: React.CSSProperties = {
            position: "absolute",
            top: 0,
            left: 0,
            height: ROW,
            transform: `translateY(${String(v.start)}px)`,
            display: "flex",
            fontSize: 13,
            borderBottom: "1px solid var(--mantine-color-default-border)",
          };
          if (line.kind === "items") {
            return (
              <div
                key={`h:${line.album.id}`}
                role="row"
                style={{
                  ...style,
                  minWidth: width("item"),
                  fontWeight: 600,
                  color: "var(--mantine-color-dimmed)",
                }}
              >
                <Cellbox w={SELECT + EXPAND} />
                {props.columns.item.map((c) => (
                  <HeaderCell key={c} {...props} scope="item" field={c} />
                ))}
              </div>
            );
          }
          const s: Scope = line.row.target.scope;
          return (
            <GridRow
              key={line.row.key}
              line={line}
              style={{ ...style, minWidth: width(s) }}
              indent={scope === "album" && s === "item"}
              {...props}
            />
          );
        })}
      </div>
      {all.length === 0 && (
        <Text c="dimmed" size="sm" p="sm">
          Nothing matches.
        </Text>
      )}
    </Box>
  );
}

function Cellbox({ w, children }: { w: number; children?: React.ReactNode }) {
  return (
    <div
      style={{
        width: w,
        minWidth: w,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {children}
    </div>
  );
}

function HeaderCell({ scope, field, ...props }: { scope: Scope; field: string } & GridProps) {
  const w = props.widths[scope][field] ?? props.defaultWidth;
  const start = useRef<{ x: number; w: number } | null>(null);
  return (
    <div
      role="columnheader"
      style={{
        width: w,
        minWidth: w,
        padding: "0 8px",
        display: "flex",
        alignItems: "center",
        position: "relative",
        fontFamily: "var(--mantine-font-family-monospace)",
      }}
    >
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {field}
      </span>
      <div
        aria-hidden
        data-testid={`resize-${field}`}
        onPointerDown={(e) => {
          start.current = { x: e.clientX, w };
          try {
            e.currentTarget.setPointerCapture(e.pointerId);
          } catch {
            // jsdom and old browsers: resizing still follows the pointer over the handle
          }
        }}
        onPointerMove={(e) => {
          if (start.current === null) return;
          const next = Math.max(MIN_WIDTH, start.current.w + e.clientX - start.current.x);
          props.onResize(scope, field, Math.round(next));
        }}
        onPointerUp={() => {
          start.current = null;
        }}
        style={{
          position: "absolute",
          right: 0,
          top: 0,
          bottom: 0,
          width: 6,
          cursor: "col-resize",
        }}
      />
    </div>
  );
}

function GridRow({
  line,
  style,
  indent,
  ...props
}: {
  line: Exclude<Line, { kind: "items" }>;
  style: React.CSSProperties;
  indent: boolean;
} & GridProps) {
  const { row } = line;
  const s = row.target.scope;
  const failure = props.failures.get(row.key);
  const rowProblem = props.problems.get(cellKey(row.key, ""));
  return (
    <div
      role="row"
      data-testid="grid-row"
      data-row={row.key}
      data-busy={props.busy.has(row.key) ? "true" : undefined}
      style={{
        ...style,
        background: failure || rowProblem ? "var(--mantine-color-red-light)" : undefined,
        opacity: props.busy.has(row.key) ? 0.6 : undefined,
      }}
      title={failure ?? rowProblem}
    >
      <Cellbox w={indent ? SELECT + EXPAND / 2 : SELECT}>
        <Checkbox
          size="xs"
          aria-label={`Select ${row.key}`}
          checked={props.selected.has(row.key)}
          onChange={(e) => {
            props.onSelect([row], e.currentTarget.checked);
          }}
        />
      </Cellbox>
      <Cellbox w={indent ? EXPAND / 2 : EXPAND}>
        {line.kind === "album" && (line.album.items ?? []).length > 0 && (
          <ActionIcon
            size="sm"
            variant="subtle"
            aria-label={props.expanded.has(row.album) ? "Collapse" : "Expand"}
            aria-expanded={props.expanded.has(row.album)}
            onClick={() => {
              props.onToggle(row.album);
            }}
          >
            {props.expanded.has(row.album) ? "▾" : "▸"}
          </ActionIcon>
        )}
      </Cellbox>
      {props.columns[s].map((field) => (
        <Cell key={field} row={row} field={field} {...props} />
      ))}
    </div>
  );
}

function Cell({ row, field, ...props }: { row: Row; field: string } & GridProps) {
  const [open, setOpen] = useState(false);
  const w = props.widths[row.target.scope][field] ?? props.defaultWidth;
  const key = cellKey(row.key, field);
  const edit = props.pending[row.key]?.changes[field];
  const problem = props.problems.get(key);
  const conflict = props.conflicts.get(key);
  const current = row.fields[field];
  const editable = !props.busy.has(row.key);
  const bg = problem
    ? "var(--mantine-color-red-light)"
    : conflict
      ? "var(--mantine-color-orange-light)"
      : edit
        ? "var(--mantine-color-yellow-light)"
        : undefined;
  return (
    <Popover opened={open} onChange={setOpen} position="bottom-start" withinPortal trapFocus>
      <Popover.Target>
        <div
          role="gridcell"
          tabIndex={0}
          data-testid="cell"
          data-field={field}
          data-pending={edit ? "true" : undefined}
          data-problem={problem ? "true" : undefined}
          data-conflict={conflict ? "true" : undefined}
          aria-invalid={problem ? true : undefined}
          title={problem ?? (conflict ? `now ${show(conflict.current)}` : show(current))}
          onDoubleClick={() => {
            if (editable) setOpen(true);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && editable) {
              e.preventDefault();
              setOpen(true);
            }
          }}
          style={{
            width: w,
            minWidth: w,
            padding: "0 8px",
            display: "flex",
            alignItems: "center",
            overflow: "hidden",
            whiteSpace: "nowrap",
            textOverflow: "ellipsis",
            background: bg,
            cursor: editable ? "text" : undefined,
          }}
        >
          {edit ? (
            <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
              <s style={{ opacity: 0.6 }}>{show(edit.old) || "(none)"}</s> →{" "}
              <b>{edit.op === "delete" ? "(deleted)" : show(edit.new) || "(empty)"}</b>
            </span>
          ) : conflict ? (
            <span>
              now <b>{show(conflict.current) || "(none)"}</b>, you wanted{" "}
              <b>{conflict.wanted.op === "delete" ? "(deleted)" : show(conflict.wanted.new)}</b>
            </span>
          ) : (
            <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{show(current)}</span>
          )}
        </div>
      </Popover.Target>
      <Popover.Dropdown>
        {open && (
          <CellEditor
            row={row}
            field={field}
            edit={edit}
            problem={problem}
            isList={Array.isArray(current) || props.lists.has(field)}
            lists={props.lists}
            onDone={(next) => {
              props.onEdit(row, field, next);
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

function initialText(edit: Edit | undefined, current: FieldValue | undefined): string {
  if (edit?.op === "set") return Array.isArray(edit.new) ? "" : show(edit.new);
  return Array.isArray(current) ? "" : show(current);
}

function initialList(edit: Edit | undefined, current: FieldValue | undefined): string[] {
  const value = edit?.op === "set" ? edit.new : current;
  if (Array.isArray(value)) return value;
  return value === null || value === undefined || value === "" ? [] : [String(value)];
}

/** The in-place editor: text, or a list of entries for multi-value fields (TAG-GRID-5). */
function CellEditor({
  row,
  field,
  edit,
  problem,
  isList,
  lists,
  onDone,
  onCancel,
}: {
  row: Row;
  field: string;
  edit: Edit | undefined;
  problem: string | undefined;
  isList: boolean;
  lists: Set<string>;
  onDone: (edit: Edit | null) => void;
  onCancel: () => void;
}) {
  const current = row.fields[field];
  const [text, setText] = useState(() => initialText(edit, current));
  const [entries, setEntries] = useState(() => initialList(edit, current));
  const commit = () => {
    if (isList) onDone({ op: "set", old: current ?? null, new: entries });
    else onDone(setEdit(row, field, text, lists));
  };
  return (
    <Stack gap="xs" w={280} data-testid="cell-editor">
      <Text size="xs" c="dimmed" ff="monospace">
        {field}
      </Text>
      {isList ? (
        <TagsInput
          aria-label={`${field} entries`}
          data-autofocus
          value={entries}
          onChange={setEntries}
          splitChars={[]}
          acceptValueOnBlur
          clearable
          description="One value per entry; press Enter to add one"
        />
      ) : (
        <TextInput
          aria-label={`${field} value`}
          data-autofocus
          value={text}
          onChange={(e) => {
            setText(e.currentTarget.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commit();
            } else if (e.key === "Escape") {
              onCancel();
            }
          }}
        />
      )}
      {problem && (
        <Text size="xs" c="red">
          {problem}
        </Text>
      )}
      <Group gap="xs" justify="space-between">
        <Group gap="xs">
          <Button size="xs" onClick={commit}>
            Done
          </Button>
          <Button size="xs" variant="default" onClick={onCancel}>
            Cancel
          </Button>
        </Group>
        <Group gap="xs">
          {edit !== undefined && (
            <Tooltip label="Discard this pending edit">
              <Button
                size="xs"
                variant="subtle"
                onClick={() => {
                  onDone(null);
                }}
              >
                Revert
              </Button>
            </Tooltip>
          )}
          {current !== undefined && current !== null && (
            <Button
              size="xs"
              variant="subtle"
              color="red"
              onClick={() => {
                onDone(deleteEdit(row, field));
              }}
            >
              Delete field
            </Button>
          )}
        </Group>
      </Group>
    </Stack>
  );
}
