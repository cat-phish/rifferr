import { Badge } from "@mantine/core";

const COLORS: Record<string, string> = {
  queued: "gray",
  pending: "gray",
  running: "blue",
  succeeded: "green",
  failed: "red",
  cancelled: "orange",
  skipped: "gray",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <Badge color={COLORS[status] ?? "gray"} variant="light">
      {status}
    </Badge>
  );
}
