import { Alert, Badge, Button, Card, Group, Image, SimpleGrid, Stack, Text } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ok } from "../api/client";
import type { CoverReview, RankedCandidate } from "../api/types";

type Body = { decision: "choose" | "skip" | "fail"; candidate_id?: string };

/** A candidate's image through the server, which only serves candidates it found. */
export function candidateImage(profile: string, id: string, thumb = true): string {
  const params = new URLSearchParams({ profile });
  if (thumb) params.set("thumb", "1");
  return `/api/covers/image/${encodeURIComponent(id)}?${params.toString()}`;
}

export function size(c: RankedCandidate): string {
  return c.width != null && c.height != null
    ? `${String(c.width)}×${String(c.height)}`
    : "size unknown";
}

/** An album waiting for a cover choice (S-COVERS-SEARCH COVERS-SEARCH-21). The full
 * picker, with a new search and provider weights, is F-COVER-PICKER. */
export function CoverReviewView({ review }: { review: CoverReview }) {
  const queryClient = useQueryClient();
  const decide = useMutation({
    mutationFn: async (body: Body) =>
      ok(
        await api.POST("/api/covers/reviews/{review_id}/decision", {
          params: { path: { review_id: review.id } },
          body,
        }),
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["review"] }),
  });
  const busy = decide.isPending || decide.isSuccess;
  return (
    <Stack gap="xs" data-testid="cover-review">
      <Text size="sm">
        <b>{review.label}</b>
        {review.reason ? ` — ${review.reason}` : ""}
      </Text>
      {review.candidates.length === 0 ? (
        <Text size="sm" c="dimmed" data-testid="no-candidates">
          No provider found a cover.
        </Text>
      ) : (
        <SimpleGrid cols={{ base: 2, sm: 4 }}>
          {review.candidates.map((c) => (
            <Card key={c.id} withBorder padding="xs" data-testid="cover-candidate">
              <Card.Section>
                <Image
                  src={candidateImage(review.profile, c.id)}
                  alt={c.title ?? `${c.provider} cover`}
                  h={140}
                  fit="contain"
                />
              </Card.Section>
              <Group gap={4} mt="xs">
                <Badge size="xs" variant="light">
                  {c.provider}
                </Badge>
                <Text size="xs">{size(c)}</Text>
                <Text size="xs" c="dimmed">
                  score {c.score.toFixed(2)}
                </Text>
              </Group>
              {c.title && (
                <Text size="xs" c="dimmed" lineClamp={1}>
                  {c.title}
                </Text>
              )}
              <Button
                size="xs"
                mt="xs"
                disabled={busy}
                onClick={() => {
                  decide.mutate({ decision: "choose", candidate_id: c.id });
                }}
              >
                Use this cover
              </Button>
            </Card>
          ))}
        </SimpleGrid>
      )}
      <Group>
        <Button
          variant="default"
          size="xs"
          disabled={busy}
          onClick={() => {
            decide.mutate({ decision: "skip" });
          }}
        >
          No cover
        </Button>
        <Button
          variant="default"
          color="red"
          size="xs"
          disabled={busy}
          onClick={() => {
            decide.mutate({ decision: "fail" });
          }}
        >
          Fail this album
        </Button>
      </Group>
      {decide.error && <Alert color="red">{decide.error.message}</Alert>}
      {decide.isSuccess && (
        <Text size="sm" c="dimmed" data-testid="decision-sent">
          {decide.variables.decision === "choose" ? "Setting the cover." : "Done."}
        </Text>
      )}
    </Stack>
  );
}
