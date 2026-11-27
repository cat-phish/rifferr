import {
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  Group,
  Modal,
  Progress,
  Radio,
  ScrollArea,
  Stack,
  Text,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ok } from "../api/client";
import type { components } from "../api/schema";
import type { ChoiceReview, QueueItem, RunEvent } from "../api/types";
import { useAppEventListener } from "../lib/appEvents";

type S = components["schemas"];
export type AISuggestion = S["ChoiceAISuggestion"];
export const AI_PREFIX = "ai:";
const TASK = "choice.suggest";

/** The radio value of an AI suggestion: never an option key, so it is never preselected
 * (F-AI-SUGGEST AI-SUGGEST-13). */
export const aiValue = (s: AISuggestion) => `${AI_PREFIX}${String(s.rank)}`;

function percent(confidence: number): string {
  return `${String(Math.round(confidence * 100))}%`;
}

function labelOf(s: AISuggestion): string {
  if (!s.new) return s.label;
  return Object.values(s.values ?? {})
    .flat()
    .join(", ");
}

/** Ask the AI about one choice review (AI-SUGGEST-10). */
export function useAskAI(review: ChoiceReview) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (refresh: boolean) =>
      ok(
        await api.POST("/api/choices/{review_id}/ai-suggest", {
          params: { path: { review_id: review.id } },
          body: { refresh },
        }),
      ),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["review"] });
    },
  });
}

/** AI suggestions on a choice card, ranked under the rules' options (AI-SUGGEST-12, -13).
 * Each one is a choice like any other; none is preselected and nothing applies until the
 * person picks one and presses Apply (AI-SUGGEST-15). Render inside the card's Radio.Group. */
export function AISuggestionsSection({
  review,
  disabled,
}: {
  review: ChoiceReview;
  disabled: boolean;
}) {
  const ask = useAskAI(review);
  const [previewOpen, setPreviewOpen] = useState(false);
  const ai = review.ai ?? null;
  const suggestions = ai?.suggestions ?? [];
  const asked = ai !== null && (suggestions.length > 0 || ai.error == null);
  return (
    <Stack gap="xs" data-testid="ai-suggestions">
      <Group gap="xs" justify="space-between">
        <Text size="sm" fw={500}>
          AI suggestions
        </Text>
        <Group gap="xs">
          <Anchor
            component="button"
            type="button"
            size="xs"
            onClick={() => {
              setPreviewOpen(true);
            }}
          >
            Show what will be sent
          </Anchor>
          <Button
            size="xs"
            variant="light"
            loading={ask.isPending}
            disabled={disabled}
            onClick={() => {
              ask.mutate(asked);
            }}
          >
            {asked ? "Ask again" : "Ask AI"}
          </Button>
        </Group>
      </Group>
      {ask.error && <Alert color="red">{ask.error.message}</Alert>}
      {ai?.error != null && !ask.isPending && (
        <Alert color="yellow" data-testid="ai-error">
          AI suggestion failed: {ai.error}
        </Alert>
      )}
      {suggestions.map((s) => (
        <div key={s.rank} data-testid="ai-suggestion">
          <Radio
            value={aiValue(s)}
            disabled={disabled}
            label={
              <Group gap="xs">
                <Text size="sm" fw={500}>
                  {labelOf(s)}
                </Text>
                {s.new && (
                  <Badge size="xs" color="grape" variant="light">
                    New
                  </Badge>
                )}
                <Text size="xs" c="dimmed">
                  {percent(s.confidence)}
                </Text>
              </Group>
            }
          />
          {s.rationale && (
            <Text size="xs" c="dimmed" pl={28}>
              {s.rationale}
            </Text>
          )}
        </div>
      ))}
      {ai !== null && suggestions.length === 0 && ai.error == null && (
        <Text size="xs" c="dimmed">
          The AI had no suggestion that fits.
        </Text>
      )}
      {ai?.model != null && suggestions.length > 0 && (
        <Text size="xs" c="dimmed">
          From {ai.provider} ({ai.model}){ai.cached ? ", cached" : ""}. Nothing changes until you
          pick one and apply it.
        </Text>
      )}
      <AIPreviewModal
        review={review}
        opened={previewOpen}
        onClose={() => {
          setPreviewOpen(false);
        }}
      />
    </Stack>
  );
}

/** Exactly what the AI request would send (AI-CORE-18). */
function AIPreviewModal({
  review,
  opened,
  onClose,
}: {
  review: ChoiceReview;
  opened: boolean;
  onClose: () => void;
}) {
  const input = review.ai_input ?? null;
  return (
    <AIRequestPreview
      profile={review.profile}
      task={TASK}
      input={input}
      provider={input?.provider ?? null}
      cacheKey={`choice:${review.id}`}
      opened={opened}
      onClose={onClose}
    />
  );
}

/** The prompt an AI task sends for `input`, the provider and host, and the estimate; for
 * any task (AI-CORE-18). Sends nothing to the AI. */
export function AIRequestPreview({
  profile,
  task,
  input,
  provider,
  cacheKey,
  opened,
  onClose,
  title = "What the AI request sends",
}: {
  profile: string;
  task: string;
  input: Record<string, unknown> | null;
  provider: string | null;
  cacheKey: string;
  opened: boolean;
  onClose: () => void;
  title?: string;
}) {
  const preview = useQuery({
    queryKey: ["ai-preview", cacheKey],
    enabled: opened && input !== null,
    queryFn: async () =>
      ok(
        await api.POST("/api/ai/preview", {
          body: { profile, task, input: input ?? {}, provider },
        }),
      ),
  });
  const p = preview.data;
  return (
    <Modal opened={opened} onClose={onClose} title={title} size="lg">
      {preview.error && <Alert color="red">{preview.error.message}</Alert>}
      {p && (
        <Stack gap="xs" data-testid="ai-preview">
          <Text size="sm">
            To {p.provider} ({p.model}) at {p.host ?? "the provider's default host"}
            {p.cached ? "; the answer is cached, so nothing would be sent" : ""}.
          </Text>
          <Text size="xs" c="dimmed">
            About {p.estimated_input_tokens} input tokens, up to {p.max_output_tokens} output tokens
            {p.estimated_cost_max != null
              ? `; at most ${p.estimated_cost_max} ${p.currency ?? ""} (estimate)`
              : ""}
            .
          </Text>
          <ScrollArea.Autosize mah={400}>
            <Code block>{`${p.system}\n\n${p.user}`}</Code>
          </ScrollArea.Autosize>
        </Stack>
      )}
    </Modal>
  );
}

/** The choice reviews "Suggest with AI for all" would ask about: waiting, with an `ai`
 * block, and no suggestions yet (AI-SUGGEST-14). */
export function suggestable(items: QueueItem[]): string[] {
  return items.flatMap((i) => {
    const r = i.kind === "choice" ? i.choice_review : null;
    if (!r || r.state !== "pending" || r.ai_input == null) return [];
    return (r.ai?.suggestions ?? []).length > 0 ? [] : [r.id];
  });
}

type Job = { id: string; total: number; done: number; failed: number; finished: boolean };

/** "Suggest with AI for all" (AI-SUGGEST-14): confirm with the count and the estimated
 * tokens (and cost when priced), then follow the job's progress live, with Cancel. */
export function SuggestAllButton({ items }: { items: QueueItem[] }) {
  const ids = suggestable(items);
  const [confirm, setConfirm] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const estimate = useQuery({
    queryKey: ["ai-estimate", ids.join(",")],
    enabled: confirm,
    queryFn: async () =>
      ok(await api.POST("/api/choices/ai-suggest/estimate", { body: { ids, refresh: false } })),
  });
  const start = useMutation({
    mutationFn: async () =>
      ok(await api.POST("/api/choices/ai-suggest", { body: { ids, refresh: false } })),
    onSuccess: (res) => {
      setConfirm(false);
      if (res.job) setJob({ id: res.job, total: res.total, done: 0, failed: 0, finished: false });
    },
  });
  const cancel = useMutation({
    mutationFn: async (id: string) =>
      ok(await api.POST("/api/ai/jobs/{job_id}/cancel", { params: { path: { job_id: id } } })),
  });
  useAppEventListener((event: RunEvent) => {
    if (!job || !event.type.startsWith("ai.job.")) return;
    const data = event.data as { job?: string; done?: number; failed?: number; skipped?: number };
    if (data.job !== job.id) return;
    setJob({
      ...job,
      done: (data.done ?? 0) + (data.skipped ?? 0),
      failed: data.failed ?? 0,
      finished: event.type === "ai.job.finished",
    });
  });
  if (ids.length === 0 && (!job || job.finished)) return null;
  const e = estimate.data;
  return (
    <>
      {job && !job.finished ? (
        <Group gap="xs" data-testid="ai-bulk-progress">
          <Text size="sm">
            Asking the AI: {job.done + job.failed} of {job.total}
            {job.failed > 0 ? ` (${String(job.failed)} failed)` : ""}
          </Text>
          <Progress
            w={160}
            value={job.total ? ((job.done + job.failed) / job.total) * 100 : 0}
            aria-label="AI suggestion progress"
          />
          <Button
            size="xs"
            variant="default"
            loading={cancel.isPending}
            onClick={() => {
              cancel.mutate(job.id);
            }}
          >
            Cancel
          </Button>
        </Group>
      ) : (
        <Button
          size="xs"
          variant="light"
          onClick={() => {
            setConfirm(true);
          }}
        >
          Suggest with AI for all ({ids.length})
        </Button>
      )}
      <Modal
        opened={confirm}
        onClose={() => {
          setConfirm(false);
        }}
        title="Suggest with AI for all"
      >
        <Stack gap="sm" data-testid="ai-bulk-confirm">
          {estimate.error && <Alert color="red">{estimate.error.message}</Alert>}
          {e ? (
            <Text size="sm">
              Ask the AI about {e.total} {e.total === 1 ? "album" : "albums"}
              {e.cached > 0 ? ` (${String(e.cached)} answered from the cache)` : ""}: about{" "}
              {e.input_tokens} input tokens and up to {e.max_output_tokens} output tokens
              {e.cost_max != null ? `, at most ${e.cost_max} ${e.currency ?? ""} (estimate)` : ""}.
              The suggestions only show up on each card; nothing is applied.
            </Text>
          ) : (
            <Text size="sm">Estimating…</Text>
          )}
          {start.error && <Alert color="red">{start.error.message}</Alert>}
          <Group justify="flex-end" gap="xs">
            <Button
              size="xs"
              variant="default"
              onClick={() => {
                setConfirm(false);
              }}
            >
              Cancel
            </Button>
            <Button
              size="xs"
              disabled={!e || e.total === 0}
              loading={start.isPending}
              onClick={() => {
                start.mutate();
              }}
            >
              Ask the AI
            </Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
}
