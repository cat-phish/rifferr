import {
  Alert,
  Button,
  Code,
  Container,
  PasswordInput,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useEffect, useState } from "react";
import { authStatus, signIn } from "../api/session";

/** Shown on any 401 (UI-SHELL-1). With a password login, a sign-in form (SECURITY-18);
 * the one-time link always works too (SECURITY-21). The app never handles the token. */
export function SignedOut() {
  const [passwordLogin, setPasswordLogin] = useState(false);
  const [user, setUser] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void authStatus().then((s) => {
      setPasswordLogin(s.password_login);
    });
  }, []);

  return (
    <Container size="sm" py="xl">
      <Stack>
        <Title order={1}>Signed out</Title>
        {passwordLogin && (
          <form
            data-testid="sign-in-form"
            onSubmit={(e) => {
              e.preventDefault();
              setBusy(true);
              setError(null);
              void signIn(user, password).then((problem) => {
                setBusy(false);
                if (problem === null) window.location.reload();
                else setError(problem);
              });
            }}
          >
            <Stack maw={360}>
              <TextInput
                label="User name"
                autoComplete="username"
                value={user}
                onChange={(e) => {
                  setUser(e.currentTarget.value);
                }}
                required
              />
              <PasswordInput
                label="Password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => {
                  setPassword(e.currentTarget.value);
                }}
                required
                autoFocus
              />
              {error !== null && (
                <Alert color="red" role="alert">
                  {error}
                </Alert>
              )}
              <Button type="submit" loading={busy}>
                Sign in
              </Button>
            </Stack>
          </form>
        )}
        <Text>
          {passwordLogin ? "Or open" : "Open"} the sign-in link that <Code>rifferr serve</Code>{" "}
          printed in its terminal.
        </Text>
        <Text size="sm" c="dimmed">
          Link expired, or running Rifferr as a service? Run <Code>rifferr sign-in</Code> on the
          server for a new one-time link (<Code>--host NAME</Code> for another machine), or set a
          password with <Code>rifferr passwd</Code>.
        </Text>
      </Stack>
    </Container>
  );
}
