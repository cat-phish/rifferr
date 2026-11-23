import { Stack, Tabs, Title } from "@mantine/core";
import { useParams, useSearchParams } from "react-router";
import { OTHER_WORKFLOW } from "../lib/review";
import { ReviewPage } from "./ReviewPage";
import { RunsPage } from "./RunsPage";
import { WorkflowPage } from "./WorkflowPage";

const TABS = ["review", "runs", "details"] as const;
type Tab = (typeof TABS)[number];

/** One workflow (UI-REFRESH-23): its review items by stage, its runs and its details (steps,
 * trust and safety report, start a run), one tab each, the tab kept in `?tab=`. Items of no
 * known workflow ("Other") have only the review tab. */
export function WorkflowHubPage() {
  const { name = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const other = name === OTHER_WORKFLOW;
  const asked = params.get("tab");
  const tab: Tab = !other && TABS.includes(asked as Tab) ? (asked as Tab) : "review";
  const pick = (value: string | null) => {
    if (value === null) return;
    setParams((old) => {
      const next = new URLSearchParams(old);
      if (value === "review") next.delete("tab");
      else next.set("tab", value);
      next.delete("item");
      return next;
    });
  };
  return (
    <Stack>
      <Title order={2} data-testid="workflow-title">
        {other ? "Other" : name}
      </Title>
      <Tabs value={tab} onChange={pick} keepMounted={false}>
        <Tabs.List data-testid="workflow-tabs">
          <Tabs.Tab value="review">Review</Tabs.Tab>
          {!other && <Tabs.Tab value="runs">Runs</Tabs.Tab>}
          {!other && <Tabs.Tab value="details">Details</Tabs.Tab>}
        </Tabs.List>
        <Tabs.Panel value="review" pt="sm">
          <ReviewPage workflow={other ? "" : name} />
        </Tabs.Panel>
        {!other && (
          <Tabs.Panel value="runs" pt="sm">
            <RunsPage workflow={name} />
          </Tabs.Panel>
        )}
        {!other && (
          <Tabs.Panel value="details" pt="sm">
            <WorkflowPage />
          </Tabs.Panel>
        )}
      </Tabs>
    </Stack>
  );
}
