import { Box, Text } from "@mantine/core";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useRef } from "react";
import { clock } from "../lib/format";
import type { LogLine } from "../lib/runState";

const ROW = 20;

/**
 * The run log, virtualized: only visible rows are in the DOM (UI-SHELL-10).
 * Follows the end while the user is scrolled to the bottom.
 */
export function LogView({ lines, height = 420 }: { lines: LogLine[]; height?: number }) {
  const parent = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  // eslint-disable-next-line react-hooks/incompatible-library -- the virtualizer is meant to be used this way
  const virtualizer = useVirtualizer({
    count: lines.length,
    getScrollElement: () => parent.current,
    estimateSize: () => ROW,
    overscan: 20,
  });

  useEffect(() => {
    if (atBottom.current && lines.length > 0) virtualizer.scrollToIndex(lines.length - 1);
  }, [lines.length, virtualizer]);

  return (
    <Box
      ref={parent}
      data-testid="log"
      onScroll={(e) => {
        const el = e.currentTarget;
        atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < ROW * 2;
      }}
      style={{
        height,
        overflow: "auto",
        fontFamily: "var(--mantine-font-family-monospace)",
        fontSize: 13,
        background: "var(--mantine-color-default-hover)",
        borderRadius: 4,
      }}
    >
      {lines.length === 0 && (
        <Text c="dimmed" p="xs" size="sm">
          No output yet.
        </Text>
      )}
      <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
        {virtualizer.getVirtualItems().map((row) => {
          const line = lines[row.index];
          if (line === undefined) return null;
          return (
            <div
              key={line.seq}
              data-testid="log-line"
              data-stream={line.stream}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                right: 0,
                height: ROW,
                transform: `translateY(${String(row.start)}px)`,
                whiteSpace: "pre",
                padding: "0 8px",
                color: line.stream === "stderr" ? "var(--mantine-color-red-text)" : undefined,
              }}
            >
              <span style={{ opacity: 0.6 }}>{clock(line.ts)} </span>
              {line.step !== null && <span style={{ opacity: 0.8 }}>[{line.step}] </span>}
              {line.target !== undefined && <span style={{ opacity: 0.8 }}>{line.target} </span>}
              {line.text}
            </div>
          );
        })}
      </div>
    </Box>
  );
}
