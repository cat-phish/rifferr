import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { type Call, err, mockApi, renderApp } from "../test-utils";

type S = components["schemas"];
const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const T0 = "2026-11-23T12:00:00Z";
const RUN = "01BBBBBBBBBBBBBBBBBBBBBBBB";
const ALBUM = { scope: "album" as const, id: "album:1" };

function review(over: Partial<S["ChoiceReview"]> = {}): S["ChoiceReview"] {
  return {
    id: "c1",
    profile: "main",
    run_id: RUN,
    step_id: "album-kind",
    album_id: "album:1",
    label: "A & B - Split EP",
    message: null,
    shown: { albumartist: "A & B" },
    options: [
      {
        key: "o0",
        label: "Collaboration",
        actions: [],
        preview: [
          {
            target: ALBUM,
            field: "albumartist",
            before: "A & B",
            after: "Collaboration",
            deleted: false,
          },
        ],
        rule: false,
        matched: 0,
      },
      {
        key: "o1",
        label: "Split",
        actions: [],
        preview: [
          { target: ALBUM, field: "albumartist", before: "A & B", after: "Split", deleted: false },
        ],
        rule: false,
        matched: 0,
      },
    ],
    suggested: "o1",
    custom: [
      { field: "albumartist", kind: "text", value: "A & B" },
      { field: "albumartists", kind: "list", value: ["A", "B"] },
    ],
    known_name: "albumartist",
    known_name_value: "A & B",
    known_name_option: "Single artist",
    known_name_actions: [],
    learn: null,
    on_reject: "stop",
    state: "pending",
    created_at: T0,
    decided_by: null,
    decided_at: null,
    chosen: null,
    note: null,
    changeset_id: null,
    learned: null,
    ...over,
  };
}

function setup(r: S["ChoiceReview"], extra: Record<string, (call: Call) => unknown> = {}) {
  return mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [
      {
        busy: false,
        kind: "choice",
        id: r.id,
        profile: "main",
        run_id: RUN,
        workflow: "foobar-style",
        step_id: r.step_id,
        created_at: T0,
        choice_review: r,
      } satisfies S["QueueItem"],
    ],
    "POST /api/choices/c1/decision": () => ({ job_id: "j1" }),
    ...extra,
  });
}

const bodies = (calls: Call[]) =>
  calls
    .filter((c) => c.method === "POST" && c.path === "/api/choices/c1/decision")
    .map((c) => c.body);

test("TAGS_CHOOSE_4 options with previews, the suggestion preselected", async () => {
  const { calls } = setup(review());
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("choice-review");
  expect(within(card).getByTestId("choice-shown")).toHaveTextContent("albumartistA & B");
  const [collab, split] = within(card).getAllByTestId("choice-option");
  expect(collab).toHaveTextContent("Collaboration");
  expect(split).toHaveTextContent("suggested");
  expect(split).toHaveTextContent("albumartist: A & B → Split");
  expect(within(split as HTMLElement).getByRole("radio")).toBeChecked();
  fireEvent.click(within(collab as HTMLElement).getByRole("radio"));
  fireEvent.click(within(card).getByRole("button", { name: "Apply" }));
  await waitFor(() => {
    expect(bodies(calls)).toHaveLength(1);
  });
  expect(bodies(calls)).toEqual([
    { known_name: false, decision: "choose", option: "o0", note: null },
  ]);
});

test("TAGS_CHOOSE_4 custom entry with a list field as tag input", async () => {
  const { calls } = setup(review({ suggested: null }));
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("choice-review");
  fireEvent.click(within(within(card).getByTestId("choice-custom")).getByRole("radio"));
  fireEvent.change(within(card).getByLabelText("albumartist"), { target: { value: "Split" } });
  fireEvent.click(within(card).getByRole("button", { name: "Apply" }));
  await waitFor(() => {
    expect(bodies(calls)).toHaveLength(1);
  });
  expect(bodies(calls)).toEqual([
    {
      known_name: false,
      decision: "choose",
      option: "custom",
      values: { albumartist: "Split", albumartists: ["A", "B"] },
      note: null,
    },
  ]);
});

test("TAGS_CHOOSE_4 a custom list field suggests known values while typing", async () => {
  setup(
    review({
      suggested: null,
      custom: [
        { field: "genres", kind: "list", value: [], suggestions: ["Punk", "Pop Punk", "Rock"] },
      ],
    }),
  );
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("choice-review");
  fireEvent.click(within(within(card).getByTestId("choice-custom")).getByRole("radio"));
  const input = within(card)
    .getAllByLabelText("genres")
    .find((e) => e.tagName === "INPUT") as HTMLInputElement;
  fireEvent.focus(input);
  fireEvent.click(input);
  const options = await screen.findAllByRole("option");
  expect(options.map((o) => o.textContent)).toEqual(["Punk", "Pop Punk", "Rock"]);
});

test("TAGS_CHOOSE_13 This is one artist, and skip", async () => {
  const { calls } = setup(review());
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("choice-review");
  fireEvent.click(within(card).getByRole("button", { name: "This is one artist" }));
  await waitFor(() => {
    expect(bodies(calls)).toHaveLength(1);
  });
  expect(bodies(calls)).toEqual([{ known_name: true, decision: "choose", note: null }]);
});

test("TAGS_CHOOSE_6 skip, and errors show", async () => {
  const { calls } = setup(review({ known_name: null, known_name_value: null }), {
    "POST /api/choices/c1/decision": () => err(409, "busy", "being decided"),
  });
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("choice-review");
  expect(within(card).queryByRole("button", { name: "This is one artist" })).toBeNull();
  fireEvent.click(within(card).getByRole("button", { name: "Skip" }));
  expect(await within(card).findByText(/being decided/)).toBeInTheDocument();
  expect(bodies(calls)).toEqual([{ known_name: false, decision: "skip", note: null }]);
});
