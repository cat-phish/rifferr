import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, test } from "vitest";
import type { components } from "../api/schema";
import { candidateImage } from "../lib/covers";
import { type Call, FakeEventSource, err, mockApi, renderApp } from "../test-utils";

type S = components["schemas"];
const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const T0 = "2026-11-14T12:00:00Z";
const RUN = "01CCCCCCCCCCCCCCCCCCCCCCCC";

function candidate(id: string, over: Partial<S["RankedCandidate"]> = {}): S["RankedCandidate"] {
  return {
    id,
    provider: "itunes",
    title: "Artist - Album",
    image_url: `https://is1-ssl.mzstatic.com/${id}.jpg`,
    path: null,
    thumb_url: null,
    width: 1200,
    height: 1200,
    match_confidence: 1,
    source_url: null,
    score: 1,
    size_score: 1,
    filtered: null,
    ...over,
  };
}

function review(over: Partial<S["CoverReview"]> = {}): S["CoverReview"] {
  return {
    id: "v1",
    profile: "main",
    run_id: RUN,
    step_id: "art",
    album_id: "album-1",
    label: "Artist - Album",
    state: "pending",
    query: { artist: "Artist", album: "Album" },
    candidates: [
      candidate("c1"),
      candidate("c2", {
        provider: "deezer",
        width: null,
        height: null,
        score: 0.5,
        size_score: 0.5,
      }),
    ],
    dropped: {},
    old_sha256: null,
    reason: "no cover scored 0.6 or more",
    created_at: T0,
    decided_by: null,
    decided_at: null,
    chosen: null,
    ...over,
  };
}

const VIEW: S["CoverProvidersView"] = {
  providers: [
    { name: "itunes", enabled: true, weight: 1 },
    { name: "deezer", enabled: true, weight: 1 },
  ],
  registered: ["deezer", "itunes", "local"],
  profile_hash: "h1",
};
let searches = 0;

function setup(r: S["CoverReview"], extra: Record<string, (call: Call) => unknown> = {}) {
  return mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [
      {
        kind: "cover",
        id: r.id,
        profile: "main",
        run_id: r.run_id,
        workflow: "inbox",
        step_id: r.step_id,
        created_at: T0,
        cover_review: r,
      },
    ],
    "POST /api/covers/reviews/v1/decision": () => ({ job_id: "j1", sha256: "a".repeat(64) }),
    "GET /api/profiles/main/covers": () => VIEW,
    "POST /api/covers/search": () => ({
      status: 202,
      body: { search_id: `s${String(++searches)}` },
    }),
    "DELETE /api/covers/search/s1": () => ({ search_id: "s1" }),
    ...extra,
  });
}

const bodies = (calls: Call[], path = "/api/covers/reviews/v1/decision") =>
  calls.filter((c) => c.method !== "GET" && c.path === path).map((c) => c.body);

const STREAM = "/api/profiles/main/events";

async function openPicker() {
  const picker = await screen.findByTestId("cover-picker");
  await waitFor(() => {
    expect(within(picker).getAllByTestId("provider-row")).toHaveLength(3);
  });
  act(() => {
    FakeEventSource.last(STREAM).open();
  });
  return picker;
}

const order = (picker: HTMLElement) =>
  within(picker)
    .getAllByTestId("cover-candidate")
    .map((c) => `${c.dataset.provider ?? ""} ${within(c).getByTestId("score").textContent}`);

beforeEach(() => {
  searches = 0;
});

test("COVER_PICKER_1 image links go through the server", () => {
  expect(candidateImage("main", "c1")).toBe("/api/covers/image/c1?profile=main&thumb=1");
  expect(candidateImage("main", "c1", false)).toBe("/api/covers/image/c1?profile=main");
});

test("COVER_PICKER_1 the review's candidates are shown and one is chosen (COVERS_SEARCH_21)", async () => {
  const { calls } = setup(review());
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("queue-item");
  expect(within(card).getByText("Cover choice")).toBeInTheDocument();
  const picker = await openPicker();
  expect(picker).toHaveTextContent("no cover scored 0.6 or more");
  const shown = within(picker).getAllByTestId("cover-candidate");
  expect(shown).toHaveLength(2);
  expect(shown[0]).toHaveTextContent("1200×1200");
  expect(shown[1]).toHaveTextContent("size unknown");
  expect(bodies(calls, "/api/covers/search")).toEqual([]); // no search until asked
  fireEvent.click(within(shown[0] as HTMLElement).getByRole("button", { name: "Use this cover" }));
  // Decided: the item leaves the waiting list at once (UI-REFRESH-20).
  await waitFor(() => {
    expect(picker).not.toBeInTheDocument();
  });
  expect(bodies(calls)).toEqual([{ decision: "choose", candidate_id: "c1" }]);
});

test("COVER_PICKER_13 no cover, and refusals show", async () => {
  const { calls } = setup(review({ candidates: [] }), {
    "POST /api/covers/reviews/v1/decision": () => err(409, "already_resolved", "already skipped"),
  });
  renderApp("/review?profile=main");
  const picker = await openPicker();
  expect(within(picker).getByTestId("no-candidates")).toBeInTheDocument();
  fireEvent.click(within(picker).getByRole("button", { name: "No cover" }));
  expect(await within(picker).findByText("already skipped")).toBeInTheDocument();
  await waitFor(() => {
    expect(bodies(calls)).toEqual([{ decision: "skip" }]);
  });
});

test("COVER_PICKER_4 COVER_PICKER_6 COVER_PICKER_7 an edited query streams candidates and provider states, ignoring other searches", async () => {
  const { calls } = setup(review({ candidates: [] }));
  renderApp("/review?profile=main");
  const picker = await openPicker();
  fireEvent.change(within(picker).getByLabelText("Album"), {
    target: { value: "Album (Remastered)" },
  });
  fireEvent.click(within(picker).getByRole("button", { name: "Search" }));
  await waitFor(() => {
    expect(bodies(calls, "/api/covers/search")).toHaveLength(1);
  });
  expect(bodies(calls, "/api/covers/search")[0]).toMatchObject({
    profile: "main",
    query: { album: "Album (Remastered)" },
    providers: ["itunes", "deezer"],
    review_id: "v1",
  });
  const stream = FakeEventSource.last(STREAM);
  act(() => {
    stream.emit("covers.candidate", {
      type: "covers.candidate",
      data: { search_id: "s1", candidate: candidate("a") },
    });
    stream.emit("covers.candidate", {
      type: "covers.candidate",
      data: { search_id: "other", candidate: candidate("x") },
    });
    stream.emit("covers.provider", {
      type: "covers.provider",
      data: {
        search_id: "s1",
        status: { name: "deezer", state: "failed", count: 0, cached: false, message: "HTTP 503" },
      },
    });
  });
  await waitFor(() => {
    expect(within(picker).getAllByTestId("cover-candidate")).toHaveLength(1);
  });
  const statuses = within(picker).getAllByTestId("provider-status");
  expect(statuses.map((s) => s.textContent)).toEqual(["searching", "failed: HTTP 503"]);
  act(() => {
    stream.emit("covers.done", {
      type: "covers.done",
      data: {
        search_id: "s1",
        ranked: [
          candidate("a"),
          candidate("b", { score: 0.7, size_score: 0.7, width: 700, height: 700 }),
        ],
        hidden: [
          candidate("t", {
            width: 100,
            height: 100,
            filtered: "small",
            size_score: 0.1,
            score: 0.1,
          }),
        ],
        dropped: { itunes: { small: 1 } },
      },
    });
  });
  await waitFor(() => {
    expect(within(picker).getAllByTestId("cover-candidate")).toHaveLength(2);
  });
  expect(picker).toHaveTextContent("1 too small hidden");
  fireEvent.click(within(picker).getByLabelText("Show hidden (1)"));
  expect(within(picker).getAllByTestId("cover-candidate")).toHaveLength(3);
});

test("COVER_PICKER_5 a new search cancels the running one", async () => {
  const { calls } = setup(review({ candidates: [] }));
  renderApp("/review?profile=main");
  const picker = await openPicker();
  fireEvent.click(within(picker).getByRole("button", { name: "Search" }));
  await waitFor(() => {
    expect(bodies(calls, "/api/covers/search")).toHaveLength(1);
  });
  fireEvent.click(within(picker).getByRole("button", { name: "Search again" }));
  await waitFor(() => {
    expect(calls.some((c) => c.method === "DELETE" && c.path === "/api/covers/search/s1")).toBe(
      true,
    );
  });
  expect(bodies(calls, "/api/covers/search")[1]).toMatchObject({ refresh: true });
});

test("COVER_PICKER_9 weights re-rank at once, toggles hide, and a new provider is asked alone", async () => {
  const { calls } = setup(
    review({
      candidates: [
        candidate("i", {
          provider: "itunes",
          score: 0.6,
          size_score: 0.6,
          width: 600,
          height: 600,
        }),
        candidate("d", { provider: "deezer", score: 0.5, size_score: 1, match_confidence: 0.5 }),
      ],
    }),
  );
  renderApp("/review?profile=main");
  const picker = await openPicker();
  expect(order(picker)).toEqual(["itunes score 0.60", "deezer score 0.50"]);
  const deezer = within(picker).getAllByTestId("provider-row")[1] as HTMLElement;
  const slider = within(deezer).getByRole("slider");
  slider.focus();
  fireEvent.keyDown(slider, { key: "End" }); // weight 10
  expect(order(picker)).toEqual(["deezer score 5.00", "itunes score 0.60"]);
  expect(bodies(calls, "/api/covers/search")).toEqual([]); // no new search
  fireEvent.click(within(deezer).getByRole("switch"));
  expect(order(picker)).toEqual(["itunes score 0.60"]);

  fireEvent.click(within(picker).getByRole("button", { name: "Search" }));
  await waitFor(() => {
    expect(bodies(calls, "/api/covers/search")).toHaveLength(1);
  });
  expect(bodies(calls, "/api/covers/search")[0]).toMatchObject({ providers: ["itunes"] });
  const local = within(picker).getAllByTestId("provider-row")[2] as HTMLElement;
  fireEvent.click(within(local).getByRole("switch"));
  await waitFor(() => {
    expect(bodies(calls, "/api/covers/search")).toHaveLength(2);
  });
  expect(bodies(calls, "/api/covers/search")[1]).toMatchObject({ providers: ["local"] });
});

test("COVER_PICKER_10 COVER_PICKER_11 save as defaults sends the order, toggles and weights", async () => {
  const { calls } = setup(review(), {
    "PUT /api/profiles/main/covers/providers": () => ({ ...VIEW, profile_hash: "h2" }),
  });
  renderApp("/review?profile=main");
  const picker = await openPicker();
  fireEvent.click(within(picker).getByRole("button", { name: "Move deezer up" }));
  fireEvent.click(within(picker).getByRole("button", { name: "Save as defaults" }));
  expect(await within(picker).findByTestId("saved")).toBeInTheDocument();
  expect(bodies(calls, "/api/profiles/main/covers/providers")).toEqual([
    {
      providers: [
        { name: "deezer", enabled: true, weight: 1 },
        { name: "itunes", enabled: true, weight: 1 },
        { name: "local", enabled: false, weight: 1 },
      ],
      profile_hash: "h1",
    },
  ]);
});

test("COVER_PICKER_10 a changed profile.yaml is reported", async () => {
  setup(review(), {
    "PUT /api/profiles/main/covers/providers": () => err(409, "conflict"),
  });
  renderApp("/review?profile=main");
  const picker = await openPicker();
  fireEvent.click(within(picker).getByRole("button", { name: "Save as defaults" }));
  expect(await within(picker).findByText(/profile.yaml changed/)).toBeInTheDocument();
});

function paste(target: HTMLElement, files: File[], types: string[]) {
  fireEvent.paste(target, { clipboardData: { files, items: [], types } });
}

test("COVER_PICKER_17 a pasted image is added as a candidate", async () => {
  const added = candidate("u1", { provider: "upload", title: "Added image" });
  const { calls } = setup(review({ candidates: [] }), {
    "POST /api/covers/uploads": () => ({ status: 201, body: { candidate: added } }),
  });
  renderApp("/review?profile=main");
  const picker = await openPicker();
  const png = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], "image.png", {
    type: "image/png",
  });
  paste(picker, [png], ["Files"]);
  await waitFor(() => {
    expect(within(picker).getAllByTestId("cover-candidate")).toHaveLength(1);
  });
  const upload = calls.find((c) => c.path === "/api/covers/uploads");
  expect(upload?.search.get("profile")).toBe("main");
  expect(upload?.body).toEqual({ data: "iVBORw==" });
});

test("COVER_PICKER_19 a pasted link is refused without a request", async () => {
  const { calls } = setup(review({ candidates: [] }));
  renderApp("/review?profile=main");
  const picker = await openPicker();
  paste(picker, [], ["text/plain"]);
  expect(await within(picker).findByTestId("picker-message")).toHaveTextContent(
    "Paste an image, not a link",
  );
  expect(calls.some((c) => c.path === "/api/covers/uploads")).toBe(false);
});

test("COVER_PICKER_18 a bad image is reported", async () => {
  setup(review({ candidates: [] }), {
    "POST /api/covers/uploads": () => err(422, "bad_image", "not a JPEG or PNG image"),
  });
  renderApp("/review?profile=main");
  const picker = await openPicker();
  paste(picker, [new File(["GIF89a"], "a.gif", { type: "image/gif" })], ["Files"]);
  expect(await within(picker).findByTestId("picker-message")).toHaveTextContent(
    "not a JPEG or PNG image",
  );
});

test("COVER_PICKER_12 COVER_PICKER_15 focusable candidates with alt text; a failed image can't be chosen", async () => {
  setup(review(), {
    "GET /api/covers/image/c1": () => err(502, "image_unavailable", "itunes: HTTP 404"),
  });
  renderApp("/review?profile=main");
  const picker = await openPicker();
  expect(within(picker).getByRole("img", { name: "itunes cover, 1200×1200" })).toBeInTheDocument();
  const first = within(picker).getAllByRole("button", {
    name: /^Preview itunes cover/,
  })[0] as HTMLElement;
  first.focus();
  expect(first).toHaveFocus();
  fireEvent.click(first);
  const preview = await screen.findByTestId("cover-preview");
  expect(await within(preview).findByTestId("preview-failed")).toHaveTextContent(
    "itunes: HTTP 404",
  );
  expect(within(preview).getByRole("button", { name: "Use this cover" })).toBeDisabled();
});
