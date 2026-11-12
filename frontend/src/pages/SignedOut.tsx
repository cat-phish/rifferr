import { Code, Container, Stack, Text, Title } from "@mantine/core";

/** Shown on any 401 (UI-SHELL-1). The app never handles the token itself. */
export function SignedOut() {
  return (
    <Container size="sm" py="xl">
      <Stack>
        <Title order={1}>Signed out</Title>
        <Text>
          Open the sign-in link that <Code>rifferr serve</Code> printed in its terminal.
        </Text>
        <Text size="sm" c="dimmed">
          Running Rifferr as a service? The server log names the token file; sign in at{" "}
          <Code>/auth/exchange?token=&lt;token&gt;</Code>.
        </Text>
      </Stack>
    </Container>
  );
}
