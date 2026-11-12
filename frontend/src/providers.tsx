import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { ApiError } from "./api/client";

export function newQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Client errors (404, 401, …) won't fix themselves by retrying.
        retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 2,
      },
    },
  });
}

/** Mantine (light/dark follows the OS, UI-SHELL-12) and TanStack Query. */
export function Providers({
  client,
  children,
  env = "default",
}: {
  client: QueryClient;
  children: ReactNode;
  /** "test" turns off Mantine transitions and portals. */
  env?: "default" | "test";
}) {
  return (
    <MantineProvider defaultColorScheme="auto" env={env}>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </MantineProvider>
  );
}
