import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { PreviewResponse, ScriptDetail } from "../api/types";
import { type Call, err, mockApi, renderApp } from "../test-utils";

const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const TEXT = "actions:\n  - name: ep-suffix\n    set: { album: '$album (EP)' }\n";

function script(over: Partial<ScriptDetail> = {}): ScriptDetail {
  return {
    name: "ep",
    file: "ep.yaml",
    sha256: "a".repeat(64),
    valid: true,
    errors: [],
    text: TEXT,
    ...over,
  };
}

function response(over: Partial<PreviewResponse> = {}): PreviewResponse {
  return {
    albums: 1,
    truncated: false,
    idempotence_checked: true,
    validation_errors: [],
    idempotence: [],
    targets: [
      {
        target: { scope: "album", id: "album-1" },
        album: "album-1",
        label: "Band - Live",
        fields: [
          {
            field: "album",
            before: "Live",
            after: "Live (EP)",
            deleted: false,
            actions: ["ep-suffix"],
          },
        ],
        actions: ["ep-suffix"],
        errors: [],
        notes: [],
      },
    ],
    ...over,
  };
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function setup(preview: (call: Call) => unknown = () => response()) {
  return mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/profiles/main/scripts": () => [script()],
    "GET /api/profiles/main/scripts/ep": () => script(),
    "GET /api/profiles/main/scripts/fresh": () => err(404, "not_found"),
    "POST /api/profiles/main/tagscript/preview": preview,
    "PUT /api/profiles/main/scripts/ep": (call) =>
      script({ text: (call.body as { text: string }).text, sha256: "b".repeat(64) }),
    "PUT /api/profiles/main/scripts/fresh": (call) =>
      script({ name: "fresh", text: (call.body as { text: string }).text }),
  });
}

const previews = (calls: Call[]) =>
  calls.filter((c) => c.path === "/api/profiles/main/tagscript/preview");

test("TAGSCRIPT_22 lists script files", async () => {
  setup();
  renderApp("/scripts?profile=main");
  const table = await screen.findByTestId("scripts");
  expect(within(table).getByText("ep")).toBeInTheDocument();
  expect(within(table).getByText("valid")).toBeInTheDocument();
});

test("TAGSCRIPT_22 shows the preview grid with changed cells highlighted", async () => {
  const { calls } = setup();
  renderApp("/scripts/ep?profile=main");
  const grid = await screen.findByTestId("preview-grid");
  const row = within(grid).getByTestId("preview-row");
  expect(row).toHaveTextContent("Band - Live");
  expect(row).toHaveTextContent("Live (EP)");
  expect(within(row).getByText("Live (EP)")).toHaveAttribute("data-changed", "true");
  expect(within(row).getByText("ep-suffix")).toBeInTheDocument();
  expect(previews(calls)[0]?.body).toMatchObject({ script_text: TEXT, query: "" });
  expect(screen.getByText("idempotent")).toBeInTheDocument();
});

test("TAGSCRIPT_22 preview updates 300 ms after the last edit", async () => {
  const { calls } = setup();
  renderApp("/scripts/ep?profile=main");
  await screen.findByTestId("preview-grid");
  const before = previews(calls).length;
  const box = screen.getByRole("textbox", { name: "Script" });
  fireEvent.change(box, { target: { value: TEXT + "#1\n" } });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(100);
  });
  fireEvent.change(box, { target: { value: TEXT + "#12\n" } });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(200);
  });
  expect(previews(calls)).toHaveLength(before); // still typing
  await act(async () => {
    await vi.advanceTimersByTimeAsync(150);
  });
  await waitFor(() => {
    expect(previews(calls)).toHaveLength(before + 1);
  });
  expect(previews(calls).at(-1)?.body).toMatchObject({ script_text: TEXT + "#12\n" });
  fireEvent.change(screen.getByRole("textbox", { name: "Query" }), {
    target: { value: "albumtype:ep" },
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(350);
  });
  await waitFor(() => {
    expect(previews(calls).at(-1)?.body).toMatchObject({ query: "albumtype:ep" });
  });
});

test("TAGSCRIPT_22 a second-pass difference highlights the action and its cells", async () => {
  setup(() =>
    response({
      idempotence: [
        {
          target: { scope: "album", id: "album-1" },
          field: "album",
          action: "ep-suffix",
          action_index: 0,
          first: "Live (EP)",
          second: "Live (EP) (EP)",
        },
      ],
    }),
  );
  renderApp("/scripts/ep?profile=main");
  const warning = await screen.findByTestId("idempotence");
  expect(warning).toHaveTextContent("ep-suffix");
  expect(warning).toHaveTextContent("Live (EP) (EP)");
  const row = screen.getByTestId("preview-row");
  expect(within(row).getByText("Live (EP)")).toHaveAttribute("data-flagged", "true");
  expect(within(row).getByText("ep-suffix").closest("[data-responsible]")).not.toBeNull();
});

test("TAGSCRIPT_22 validation errors show line and column", async () => {
  setup(() =>
    response({
      targets: [],
      validation_errors: [{ path: "actions[0]", message: "unknown key", line: 2, column: 5 }],
    }),
  );
  renderApp("/scripts/ep?profile=main");
  const box = await screen.findByTestId("preview-validation");
  expect(box).toHaveTextContent("2:5: actions[0]: unknown key");
});

test("TAGSCRIPT_22 save sends the loaded hash; a changed file asks to reload", async () => {
  const { calls } = mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/profiles/main/scripts/ep": () => script(),
    "POST /api/profiles/main/tagscript/preview": () => response(),
    "PUT /api/profiles/main/scripts/ep": () => err(409, "hash_mismatch"),
  });
  renderApp("/scripts/ep?profile=main");
  const box = await screen.findByRole("textbox", { name: "Script" });
  fireEvent.change(box, { target: { value: TEXT + "# mine\n" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByText("The file changed on disk")).toBeInTheDocument();
  const put = calls.find((c) => c.method === "PUT");
  expect(put?.body).toEqual({ text: TEXT + "# mine\n", sha256: "a".repeat(64) });
});

test("TAGSCRIPT_22 a new script starts from a template and saves without a hash", async () => {
  const { calls } = setup();
  renderApp("/scripts/fresh?profile=main");
  const box = await screen.findByRole("textbox", { name: "Script" });
  expect((box as HTMLTextAreaElement).value).toContain("actions:");
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => {
    expect(calls.some((c) => c.method === "PUT")).toBe(true);
  });
  const put = calls.find((c) => c.method === "PUT");
  expect(put?.body).toMatchObject({ sha256: null });
});

test("TAG_GRID_12 a query handed over from the library fills the preview query", async () => {
  const { calls } = setup();
  renderApp("/scripts?profile=main&query=albumtype%3Aep");
  expect(await screen.findByTestId("handed-query")).toHaveTextContent("albumtype:ep");
  fireEvent.click(within(await screen.findByTestId("scripts")).getByText("ep"));
  expect(await screen.findByLabelText("Query")).toHaveValue("albumtype:ep");
  await waitFor(() => {
    expect((previews(calls).at(-1)?.body as { query: string }).query).toBe("albumtype:ep");
  });
});
