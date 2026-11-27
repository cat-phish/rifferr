import {
  Alert,
  Autocomplete,
  Badge,
  Button,
  Group,
  Radio,
  Stack,
  Table,
  TagsInput,
  Text,
  TextInput,
} from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ok } from "../api/client";
import type { ChoiceReview } from "../api/types";
import { type FieldValue, show } from "../lib/grid";
import { AI_PREFIX, AISuggestionsSection } from "./AISuggestions";
import { jobOf, useMarkDecided } from "./Decided";
import { NoteField } from "./NoteField";

const CUSTOM = "custom";

type Body = {
  decision: "choose" | "skip" | "reject";
  option?: string;
  values?: Record<string, FieldValue>;
  known_name?: boolean;
  note?: string;
  ai_suggestion?: number;
};

function initialValues(review: ChoiceReview): Record<string, FieldValue> {
  const out: Record<string, FieldValue> = {};
  for (const c of review.custom ?? []) {
    const v = c.value ?? null;
    out[c.field] =
      c.kind === "list" ? (Array.isArray(v) ? v : v === null ? [] : [String(v)]) : show(v);
  }
  return out;
}

/** An album waiting for a choice (S-TAGS-CHOOSE TAGS-CHOOSE-4): each option with a preview
 * of its field changes, the suggestion preselected, and free entry for the custom fields
 * (lists as tag inputs). Values render as text (REVIEW-QUEUE-17). */
export function ChoiceReviewView({ review }: { review: ChoiceReview }) {
  const queryClient = useQueryClient();
  const markDecided = useMarkDecided();
  // Rules the AI named appear with its suggestions, not as rules options (AI-SUGGEST-13).
  const options = review.options.filter((o) => !o.from_ai);
  const custom = review.custom ?? [];
  const [picked, setPicked] = useState<string>(
    review.suggested ?? (options.length === 0 && custom.length > 0 ? CUSTOM : ""),
  );
  const [values, setValues] = useState<Record<string, FieldValue>>(() => initialValues(review));
  const [note, setNote] = useState("");
  const decide = useMutation({
    mutationFn: async (body: Body) =>
      ok(
        await api.POST("/api/choices/{review_id}/decision", {
          params: { path: { review_id: review.id } },
          body: { known_name: false, ...body, note: note.trim() || null },
        }),
      ),
    onSuccess: (res) => {
      markDecided(jobOf(res));
      void queryClient.invalidateQueries({ queryKey: ["review"] });
    },
  });
  const choose = () => {
    if (picked.startsWith(AI_PREFIX)) {
      // An AI suggestion is an ordinary decision that says it was one (AI-SUGGEST-6).
      const rank = Number(picked.slice(AI_PREFIX.length));
      const s = (review.ai?.suggestions ?? []).find((x) => x.rank === rank);
      if (!s) return;
      if (s.option) decide.mutate({ decision: "choose", option: s.option, ai_suggestion: rank });
      else
        decide.mutate({
          decision: "choose",
          option: CUSTOM,
          values: fitted(s.values ?? {}, custom),
          ai_suggestion: rank,
        });
      return;
    }
    if (picked === CUSTOM) decide.mutate({ decision: "choose", option: CUSTOM, values });
    else decide.mutate({ decision: "choose", option: picked });
  };
  const shown = Object.entries(review.shown ?? {});

  return (
    <Stack gap="sm" data-testid="choice-review">
      <Text size="sm">{review.label}</Text>
      {review.message && <Text size="sm">{review.message}</Text>}
      {shown.length > 0 && (
        <Table withRowBorders={false} verticalSpacing={2} data-testid="choice-shown">
          <Table.Tbody>
            {shown.map(([field, value]) => (
              <Table.Tr key={field}>
                <Table.Td>
                  <Text size="sm" c="dimmed" ff="monospace">
                    {field}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Text size="sm">{show(value)}</Text>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
      <Radio.Group value={picked} onChange={setPicked} aria-label="Options">
        <Stack gap="xs">
          {options.map((o) => (
            <div key={o.key} data-testid="choice-option">
              <Radio
                value={o.key}
                disabled={decide.isPending || decide.isSuccess}
                label={
                  <Group gap="xs">
                    <Text size="sm" fw={500}>
                      {o.label}
                    </Text>
                    {o.key === review.suggested && (
                      <Badge size="xs" variant="light">
                        suggested
                      </Badge>
                    )}
                    {o.matched > 1 && (
                      <Text size="xs" c="dimmed">
                        matches {o.matched} values
                      </Text>
                    )}
                  </Group>
                }
              />
              {o.description && (
                <Text size="xs" c="dimmed" pl={28} data-testid="choice-description">
                  {o.description}
                </Text>
              )}
              <Stack gap={0} pl={28} data-testid="choice-preview">
                {o.remembers != null && (
                  <Text size="xs" c="dimmed" data-testid="choice-remembers">
                    Remembers &ldquo;{o.remembers}&rdquo; as one artist name, so it isn&rsquo;t
                    asked again
                  </Text>
                )}
                {(o.preview ?? []).map((p) => (
                  <Text key={`${p.target.id}:${p.field}`} size="xs" c="dimmed" ff="monospace">
                    {p.target.scope === "item" ? `${p.target.id} ` : ""}
                    {p.field}: {show(p.before ?? null) || "(empty)"} →{" "}
                    {p.deleted ? "(deleted)" : show(p.after ?? null) || "(empty)"}
                  </Text>
                ))}
              </Stack>
            </div>
          ))}
          {custom.length > 0 && (
            <div data-testid="choice-custom">
              <Radio
                value={CUSTOM}
                disabled={decide.isPending || decide.isSuccess}
                label={
                  <Text size="sm" fw={500}>
                    Custom
                  </Text>
                }
              />
              {picked === CUSTOM && (
                <Stack gap="xs" pl={28} pt={4}>
                  {custom.map((c) =>
                    c.kind === "list" ? (
                      <TagsInput
                        key={c.field}
                        size="xs"
                        label={c.field}
                        value={Array.isArray(values[c.field]) ? (values[c.field] as string[]) : []}
                        onChange={(next) => {
                          setValues({ ...values, [c.field]: next });
                        }}
                        data={c.suggestions ?? []}
                        limit={20}
                        splitChars={[]}
                        acceptValueOnBlur
                        clearable
                        description={hint(c.suggestions, review.learn != null)}
                      />
                    ) : (c.suggestions ?? []).length > 0 ? (
                      <Autocomplete
                        key={c.field}
                        size="xs"
                        label={c.field}
                        data={c.suggestions ?? []}
                        limit={20}
                        value={show(values[c.field] ?? null)}
                        onChange={(next) => {
                          setValues({ ...values, [c.field]: next });
                        }}
                        description={hint(c.suggestions, review.learn != null)}
                      />
                    ) : (
                      <TextInput
                        key={c.field}
                        size="xs"
                        label={c.field}
                        value={show(values[c.field] ?? null)}
                        onChange={(e) => {
                          setValues({ ...values, [c.field]: e.currentTarget.value });
                        }}
                      />
                    ),
                  )}
                </Stack>
              )}
            </div>
          )}
          {review.ai_input != null && (
            <AISuggestionsSection review={review} disabled={decide.isPending || decide.isSuccess} />
          )}
        </Stack>
      </Radio.Group>
      {decide.error && <Alert color="red">{decide.error.message}</Alert>}
      {decide.isSuccess ? (
        <Text size="sm" c="dimmed" data-testid="decision-sent">
          {decide.variables.decision === "choose"
            ? "Applying the choice."
            : decide.variables.decision === "skip"
              ? "Left unchanged."
              : "Rejected."}
        </Text>
      ) : (
        <>
          <NoteField value={note} onChange={setNote} disabled={decide.isPending} />
          <Group gap="xs" justify="flex-end">
            <Button
              size="xs"
              disabled={decide.isPending || picked === ""}
              loading={decide.isPending && decide.variables.decision === "choose"}
              onClick={choose}
            >
              Apply
            </Button>
            <Button
              size="xs"
              variant="default"
              disabled={decide.isPending}
              onClick={() => {
                decide.mutate({ decision: "skip" });
              }}
            >
              Skip
            </Button>
            <Button
              size="xs"
              variant="light"
              color="red"
              disabled={decide.isPending}
              onClick={() => {
                decide.mutate({ decision: "reject" });
              }}
            >
              Reject
            </Button>
          </Group>
        </>
      )}
    </Stack>
  );
}

/** The custom input's help (TAGS-CHOOSE-4, -10): known values drop down while typing. */
function hint(suggestions: string[] | undefined, learns: boolean): string {
  if ((suggestions ?? []).length === 0) return "One value per entry; press Enter to add one";
  return learns
    ? "Type to pick a known value, or a new one (remembered); press Enter to add"
    : "Type to pick a known value, or a new one; press Enter to add";
}

/** A new AI value comes as a list; a text custom field takes it as one string. */
function fitted(
  values: Record<string, string[]>,
  custom: ChoiceReview["custom"],
): Record<string, FieldValue> {
  const out: Record<string, FieldValue> = {};
  for (const [field, v] of Object.entries(values)) {
    const kind = (custom ?? []).find((c) => c.field === field)?.kind;
    out[field] = kind === "list" ? v : v.join("; ");
  }
  return out;
}
