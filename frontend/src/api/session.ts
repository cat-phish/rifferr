/** Sign-in and sign-out (F-SECURITY SECURITY-16, -18). These routes belong to the security
 * middleware, not the API, so they're plain same-origin fetches. */

export type AuthStatus = { signed_in: boolean; password_login: boolean };

const JSON_HEADERS = { "Content-Type": "application/json" };

/** Absolute, so a Request can be built from it anywhere (as `api` does). */
const url = (path: string): string => new URL(path, window.location.origin).toString();

export async function authStatus(): Promise<AuthStatus> {
  const r = await fetch(url("/auth/status"), { credentials: "same-origin" });
  if (!r.ok) return { signed_in: false, password_login: false };
  return (await r.json()) as AuthStatus;
}

/** Why a sign-in failed, in plain words; null when it worked. */
export async function signIn(username: string, password: string): Promise<string | null> {
  const r = await fetch(url("/auth/sign-in"), {
    method: "POST",
    credentials: "same-origin",
    headers: JSON_HEADERS,
    body: JSON.stringify({ username, password }),
  });
  if (r.status === 204) return null;
  let code = "";
  try {
    code = ((await r.json()) as { error?: { code?: string } }).error?.code ?? "";
  } catch {
    // not JSON: fall through to the status
  }
  return signInMessage(code, r.status, r.headers.get("retry-after"));
}

export function signInMessage(code: string, status: number, retryAfter: string | null): string {
  switch (code) {
    case "bad_credentials":
      return "Wrong user name or password.";
    case "too_many_attempts": {
      const minutes = Math.max(1, Math.ceil(Number(retryAfter ?? "60") / 60));
      return `Too many failed attempts. Try again in ${String(minutes)} minutes.`;
    }
    case "https_required":
      return "Password sign-in needs https. Open Rifferr at its https address, or use a sign-in link (rifferr sign-in).";
    case "no_password":
      return "No password is set. Run rifferr passwd on the server, or use a sign-in link.";
    default:
      return `Sign-in failed (HTTP ${String(status)}).`;
  }
}

export async function signOut(everywhere: boolean): Promise<boolean> {
  const r = await fetch(url(everywhere ? "/auth/sign-out-all" : "/auth/sign-out"), {
    method: "POST",
    credentials: "same-origin",
    headers: JSON_HEADERS,
    body: "{}",
  });
  return r.status === 204;
}
