import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { type Call, err, mockApi, renderApp } from "../test-utils";

type S = components["schemas"];
const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const SECRET = "sk-test-0123456789";

function view(over: Partial<S["SettingsView"]> = {}, keyring = true): S["SettingsView"] {
  return {
    section: "ai",
    title: "AI",
    base_hash: "a".repeat(64),
    schema: {},
    values: {
      concurrency: 2,
      timeout: "60s",
      cache_ttl: "90d",
      lookalikes: null,
      providers: [
        { name: "claude", type: "anthropic", model: "m1", api_key: null, enabled: true },
        {
          name: "nas",
          type: "openai-compatible",
          model: "qwen3",
          base_url: "http://127.0.0.1:11434/v1",
          enabled: true,
        },
      ],
      suggest: { mode: "unsure", steps: {} },
    },
    ai: {
      provider_types: ["anthropic", "openai-compatible"],
      keyring,
      providers: [
        {
          name: "claude",
          key_set: false,
          key_secret: null,
          host: "api.anthropic.com",
          local: false,
        },
        { name: "nas", key_set: false, key_secret: null, host: "127.0.0.1", local: true },
      ],
      steps: [
        {
          ref: "foobar-style/genre-bucket",
          workflow: "foobar-style",
          step_id: "genre-bucket",
          mode: null,
        },
      ],
    },
    ...over,
  };
}

function setup(routes: Record<string, (call: Call) => unknown> = {}, keyring = true) {
  return mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/profiles/main/settings": () => [{ id: "ai", title: "AI" }],
    "GET /api/profiles/main/settings/ai": () => view({}, keyring),
    ...(routes as Record<string, (call: Call) => never>),
  });
}

const sent = (calls: Call[], method: string, path: string) =>
  calls.filter((c) => c.method === method && c.path === path).map((c) => c.body);

test("SETTINGS_1 Settings in the nav opens the AI section", async () => {
  setup();
  renderApp("/settings?profile=main");
  expect(await screen.findByRole("tab", { name: "AI" })).toBeInTheDocument();
  expect(await screen.findByTestId("settings-ai")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Settings/ })).toBeInTheDocument();
});

test("SETTINGS_5 edit, discard and save the AI section with the loaded hash", async () => {
  const { calls } = setup({
    "PUT /api/profiles/main/settings/ai": (c) => ({
      ...view(),
      values: (c.body as { values: Record<string, never> }).values,
      base_hash: "b".repeat(64),
    }),
  });
  renderApp("/settings?profile=main");
  const page = await screen.findByTestId("settings-ai");
  const save = within(page).getByRole("button", { name: "Save" });
  expect(save).toBeDisabled();
  const [model] = within(page).getAllByLabelText("Model");
  fireEvent.change(model as HTMLElement, { target: { value: "m2" } });
  expect(save).toBeEnabled();
  fireEvent.click(within(page).getByRole("button", { name: "Discard" }));
  expect(save).toBeDisabled();
  fireEvent.click(within(page).getByRole("button", { name: "Move nas up" }));
  fireEvent.click(save);
  await waitFor(() => {
    expect(sent(calls, "PUT", "/api/profiles/main/settings/ai")).toHaveLength(1);
  });
  const body = sent(calls, "PUT", "/api/profiles/main/settings/ai")[0] as {
    values: { providers: { name: string }[] };
    base_hash: string;
  };
  expect(body.base_hash).toBe("a".repeat(64));
  expect(body.values.providers.map((p) => p.name)).toEqual(["nas", "claude"]);
  expect(within(page).getAllByTestId("remote-notice")).toHaveLength(1);
  expect(within(page).getByTestId("suggest-override")).toHaveTextContent(
    "foobar-style/genre-bucket",
  );
});

test("SETTINGS_3 a conflict offers reload or overwrite with the newer hash", async () => {
  let puts = 0;
  const { calls } = setup({
    "PUT /api/profiles/main/settings/ai": () => {
      puts += 1;
      return puts === 1 ? err(409, "conflict", "profile.yaml changed") : view();
    },
  });
  renderApp("/settings?profile=main");
  const page = await screen.findByTestId("settings-ai");
  fireEvent.change(within(page).getByLabelText("Timeout"), { target: { value: "30s" } });
  fireEvent.click(within(page).getByRole("button", { name: "Save" }));
  const notice = await screen.findByTestId("settings-conflict");
  fireEvent.click(within(notice).getByRole("button", { name: "Overwrite with my edits" }));
  await waitFor(() => {
    expect(sent(calls, "PUT", "/api/profiles/main/settings/ai")).toHaveLength(2);
  });
});

test("SETTINGS_2 field errors show at the field", async () => {
  setup({
    "PUT /api/profiles/main/settings/ai": () => ({
      status: 400,
      body: {
        error: {
          code: "invalid_settings",
          message: "invalid: timeout",
          details: [{ field: "timeout", message: "not a duration" }],
        },
      },
    }),
  });
  renderApp("/settings?profile=main");
  const page = await screen.findByTestId("settings-ai");
  fireEvent.change(within(page).getByLabelText("Timeout"), { target: { value: "soon" } });
  fireEvent.click(within(page).getByRole("button", { name: "Save" }));
  expect(await within(page).findByText("not a duration")).toBeInTheDocument();
});

test("SETTINGS_6 the key is write-only and SETTINGS_7 test connection shows the model", async () => {
  const { calls } = setup({
    "PUT /api/profiles/main/settings/ai/providers/claude/key": () => ({
      provider: "claude",
      key_set: true,
      keyring: true,
      secret: "ai-claude",
      env_var: "RIFFERR_MAIN_CLAUDE_KEY",
      env_line: "secrets: {ai-claude: env:RIFFERR_MAIN_CLAUDE_KEY}",
    }),
    "POST /api/profiles/main/settings/ai/providers/claude/test": () => ({
      ok: true,
      provider: "claude",
      model: "m1-2026",
      error: null,
    }),
  });
  renderApp("/settings?profile=main");
  const page = await screen.findByTestId("settings-ai");
  const [claude] = within(page).getAllByTestId("provider-key");
  const card = claude as HTMLElement;
  const input = within(card).getByLabelText(/API key/);
  expect(input).toHaveAttribute("type", "password");
  fireEvent.change(input, { target: { value: SECRET } });
  fireEvent.click(within(card).getByRole("button", { name: "Save key" }));
  await waitFor(() => {
    expect(sent(calls, "PUT", "/api/profiles/main/settings/ai/providers/claude/key")).toEqual([
      { key: SECRET },
    ]);
  });
  await waitFor(() => {
    expect(input).toHaveValue("");
  });
  fireEvent.click(within(card).getByRole("button", { name: "Test connection" }));
  expect(await within(card).findByTestId("test-result")).toHaveTextContent("Connected: m1-2026");
});

test("SETTINGS_6 without a keyring the page explains the environment variable", async () => {
  setup({}, false);
  renderApp("/settings?profile=main");
  const page = await screen.findByTestId("settings-ai");
  expect(within(page).getAllByTestId("no-keyring")).toHaveLength(2);
  expect(within(page).queryByLabelText(/API key/)).toBeNull();
});

test("SETTINGS_10 a new remote host is saved only after the user confirms it", async () => {
  let puts = 0;
  const { calls } = setup({
    "PUT /api/profiles/main/settings/ai": () => {
      puts += 1;
      if (puts > 1) return view();
      return {
        status: 409,
        body: {
          error: {
            code: "confirm_hosts",
            message: "album data would go to a new host",
            details: [{ host: "llm.example.net", providers: ["nas"] }],
          },
        },
      };
    },
  });
  renderApp("/settings?profile=main");
  const page = await screen.findByTestId("settings-ai");
  fireEvent.change(within(page).getAllByLabelText("Server URL")[1] as HTMLElement, {
    target: { value: "https://llm.example.net/v1" },
  });
  fireEvent.click(within(page).getByRole("button", { name: "Save" }));
  const ask = await screen.findByTestId("settings-confirm-hosts");
  expect(ask).toHaveTextContent("llm.example.net");
  expect(ask).toHaveTextContent("nas");
  expect(screen.queryByTestId("settings-conflict")).toBeNull();
  fireEvent.click(within(ask).getByRole("button", { name: "Send album data to these hosts" }));
  await waitFor(() => {
    expect(sent(calls, "PUT", "/api/profiles/main/settings/ai")).toHaveLength(2);
  });
  const [first, second] = sent(calls, "PUT", "/api/profiles/main/settings/ai") as {
    confirm_hosts?: string[];
  }[];
  expect(first?.confirm_hosts ?? []).toEqual([]);
  expect(second?.confirm_hosts).toEqual(["llm.example.net"]);
  await waitFor(() => {
    expect(screen.queryByTestId("settings-confirm-hosts")).toBeNull();
  });
});
