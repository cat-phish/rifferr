import { Badge } from "@mantine/core";
import type { StatusToken } from "../theme";
import { TokenBadge } from "./MatchBadge";

/** Final states use the status colors (UI-REFRESH-1); in-between states stay neutral. */
const TOKENS: Record<string, StatusToken> = {
  succeeded: "success",
  failed: "danger",
  cancelled: "warning",
};

export function StatusBadge({ status }: { status: string }) {
  const token = TOKENS[status];
  if (token) return <TokenBadge token={token}>{status}</TokenBadge>;
  return (
    <Badge color={status === "running" ? "blue" : "gray"} variant="light">
      {status}
    </Badge>
  );
}
