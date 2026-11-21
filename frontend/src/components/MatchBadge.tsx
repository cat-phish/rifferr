import { Badge, Group, Text } from "@mantine/core";
import { type StatusToken, tc } from "../theme";

export type MatchThresholds = { strong: number; medium: number };

/** beets' coloring of a match (UI-REFRESH-6): strong green, medium yellow, else red. */
export function matchToken(
  distance: number,
  thresholds: MatchThresholds | null,
): StatusToken | null {
  if (!thresholds) return null;
  if (distance <= thresholds.strong) return "success";
  if (distance <= thresholds.medium) return "warning";
  return "danger";
}

/** Similarity as `beet import` shows it: (1 − distance) as a percentage. */
export function similarityPercent(distance: number): string {
  return `${((1 - distance) * 100).toFixed(1)} %`;
}

/** A badge in a status color: outlined, so the token is the text and border color. */
export function TokenBadge({
  token,
  children,
  ...rest
}: {
  token: StatusToken | null;
  children: React.ReactNode;
  "data-token"?: string;
}) {
  return (
    <Badge
      variant="outline"
      color={token ? tc(token) : "gray"}
      data-token={token ?? "none"}
      style={{ textTransform: "none" }}
      {...rest}
    >
      {children}
    </Badge>
  );
}

export function MatchBadge({
  distance,
  thresholds,
}: {
  distance: number;
  thresholds: MatchThresholds | null;
}) {
  return (
    <TokenBadge token={matchToken(distance, thresholds)}>
      similarity {similarityPercent(distance)}
    </TokenBadge>
  );
}

/** beets' penalty names, in yellow, as `beet import` lists them (UI-REFRESH-7). */
export function Penalties({ names }: { names: readonly string[] }) {
  if (names.length === 0) return null;
  return (
    <Group gap={4} wrap="wrap" data-testid="penalties">
      <Text span fz="xs" style={{ color: tc("warning") }}>
        ⚠ {names.map((n) => n.replace(/_/g, " ")).join(", ")}
      </Text>
    </Group>
  );
}
