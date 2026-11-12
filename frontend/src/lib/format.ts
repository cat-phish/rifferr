export function duration(start?: string | null, end?: string | null, now = Date.now()): string {
  if (!start) return "";
  const ms = (end ? Date.parse(end) : now) - Date.parse(start);
  if (!Number.isFinite(ms) || ms < 0) return "";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${String(s)} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${String(m)} min ${String(s % 60)} s`;
  return `${String(Math.floor(m / 60))} h ${String(m % 60)} min`;
}

export function time(ts?: string | null): string {
  return ts ? new Date(ts).toLocaleString() : "";
}

export function clock(ts: string): string {
  return new Date(ts).toLocaleTimeString();
}
