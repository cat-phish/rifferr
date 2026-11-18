/** SEC-44: a provider link is shown only when it is http(s); never trust the framework to refuse the rest. */
export function webUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const u = new URL(value);
    return u.protocol === "http:" || u.protocol === "https:" ? value : null;
  } catch {
    return null;
  }
}
