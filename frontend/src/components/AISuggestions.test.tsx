import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { type Call, mockApi, renderApp } from "../test-utils";

type S = components["schemas"];
const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const T0 = "2026-11-26T12:00:00Z";
const RUN = "01BBBBBBBBBBBBBBBBBBBBBBBB";

const AI_INPUT: S["ChoiceSuggestInput"] = {
  album: "GBH - City Baby",
  message: null,
  shown: {},
  rank_by: "subgenres",
  rank_values: ["Oi"],
  options: [],
  rules: [],
  custom: ["genres"],
  context: {},
  guidance: null,
  provider: null,
  max_suggestions: 3,
  allow_new: true,
};

const SUGGESTED: S["ChoiceAI"] = {
  suggestions: [
    {
      rank: 1,
      label: "Punk",
      option: "a0",
      rule: null,
      values: {},
      new: false,
      confidence: 0.8,
      rationale: "UK82 band",
    },
    {
      rank: 2,
      label: "Street Punk",
      option: null,
      rule: null,
      values: { genres: ["Street Punk"] },
      new: true,
      confidence: 0.4,
      rationale: "",
    },
  ],
  provider: "fake",
  model: "m1",
  prompt_version: 1,
  usage: {},
  cached: false,
  auto: false,
  at: T0,
  error: null,
};

function review(over: Partial<S["ChoiceReview"]> = {}): S["ChoiceReview"] {
  return {
    id: "c1",
    profile: "main",
    run_id: RUN,
    step_id: "bucket",
    album_id: "album:1",
    label: "GBH - City Baby",
    message: null,
    shown: {},
    options: [
      {
        key: "r0",
        label: "Oi",
        description: "Working-class punk.",
        actions: [],
        preview: [],
        rule: true,
        from_ai: false,
        matched: 1,
      },
    ],
    suggested: "r0",
    custom: [{ field: "genres", kind: "list", value: [] }],
    known_name: null,
    known_name_value: null,
    known_name_option: null,
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
    ai_input: AI_INPUT,
    ai: null,
    ...over,
  };
}

const withAI = () =>
  review({
    ai: SUGGESTED,
    options: [
      ...review().options,
      {
        key: "a0",
        label: "Punk",
        actions: [],
        preview: [],
        rule: true,
        from_ai: true,
        matched: 0,
      },
    ],
  });

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

const posts = (calls: Call[], path: string) =>
  calls.filter((c) => c.method === "POST" && c.path === path).map((c) => c.body);

test("AI_SUGGEST_12 Ask AI asks once and shows the description", async () => {
  const { calls } = setup(review(), {
    "POST /api/choices/c1/ai-suggest": () => withAI(),
  });
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("choice-review");
  expect(within(card).getByTestId("choice-description")).toHaveTextContent("Working-class punk.");
  fireEvent.click(within(card).getByRole("button", { name: "Ask AI" }));
  await waitFor(() => {
    expect(posts(calls, "/api/choices/c1/ai-suggest")).toEqual([{ refresh: false }]);
  });
});

test("AI_SUGGEST_13 suggestions are never preselected; the AI rule isn't a rules option", async () => {
  setup(withAI());
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("choice-review");
  expect(within(card).getAllByTestId("choice-option")).toHaveLength(1);
  const [punk, street] = within(card).getAllByTestId("ai-suggestion");
  expect(punk).toHaveTextContent("Punk80%");
  expect(street).toHaveTextContent("Street PunkNew40%");
  for (const s of [punk, street])
    expect(within(s as HTMLElement).getByRole("radio")).not.toBeChecked();
  expect(within(card).getByRole("button", { name: "Ask again" })).toBeInTheDocument();
});

test("AI_SUGGEST_6 choosing a suggestion sends its rank", async () => {
  const { calls } = setup(withAI());
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("choice-review");
  const [, street] = within(card).getAllByTestId("ai-suggestion");
  fireEvent.click(within(street as HTMLElement).getByRole("radio"));
  fireEvent.click(within(card).getByRole("button", { name: "Apply" }));
  await waitFor(() => {
    expect(posts(calls, "/api/choices/c1/decision")).toHaveLength(1);
  });
  expect(posts(calls, "/api/choices/c1/decision")[0]).toEqual({
    known_name: false,
    decision: "choose",
    option: "custom",
    values: { genres: ["Street Punk"] },
    ai_suggestion: 2,
    note: null,
  });
});

test("AI_SUGGEST_8 a failure shows as a note", async () => {
  setup(review({ ai: { ...SUGGESTED, suggestions: [], error: "HTTP 503" } }));
  renderApp("/review?profile=main");
  expect(await screen.findByTestId("ai-error")).toHaveTextContent("AI suggestion failed: HTTP 503");
});

test("AI_SUGGEST_14 suggest for all confirms with the estimate, then starts", async () => {
  const { calls } = setup(review(), {
    "POST /api/choices/ai-suggest/estimate": () => ({
      total: 1,
      cached: 0,
      input_tokens: 900,
      max_output_tokens: 1024,
      cost_max: "0.0061",
      currency: "USD",
    }),
    "POST /api/choices/ai-suggest": () => ({ job: "job1", total: 1 }),
  });
  renderApp("/review?profile=main");
  fireEvent.click(await screen.findByRole("button", { name: "Suggest with AI for all (1)" }));
  const dialog = await screen.findByTestId("ai-bulk-confirm");
  await within(dialog).findByText(/about 900 input tokens/);
  expect(dialog).toHaveTextContent("at most 0.0061 USD");
  expect(posts(calls, "/api/choices/ai-suggest")).toEqual([]);
  fireEvent.click(within(dialog).getByRole("button", { name: "Ask the AI" }));
  expect(await screen.findByTestId("ai-bulk-progress")).toHaveTextContent("0 of 1");
  expect(posts(calls, "/api/choices/ai-suggest")).toEqual([{ ids: ["c1"], refresh: false }]);
});

test("AI_SUGGEST_1 no ai block, no AI controls", async () => {
  setup(review({ ai_input: null }));
  renderApp("/review?profile=main");
  await screen.findByTestId("choice-review");
  expect(screen.queryByTestId("ai-suggestions")).toBeNull();
  expect(screen.queryByRole("button", { name: /Suggest with AI/ })).toBeNull();
});

test("AI_SUGGEST_6 a new value for a text field is sent as one string", async () => {
  const { calls } = setup({ ...withAI(), custom: [{ field: "genres", kind: "text", value: "" }] });
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("choice-review");
  const [, street] = within(card).getAllByTestId("ai-suggestion");
  fireEvent.click(within(street as HTMLElement).getByRole("radio"));
  fireEvent.click(within(card).getByRole("button", { name: "Apply" }));
  await waitFor(() => {
    expect(posts(calls, "/api/choices/c1/decision")).toHaveLength(1);
  });
  expect(posts(calls, "/api/choices/c1/decision")[0]).toMatchObject({
    values: { genres: "Street Punk" },
  });
});
