import { Alert, Button, Group, Modal, Stack, Tabs, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams, useSearchParams } from "react-router";
import { api, ok } from "../api/client";
import { InboxFolderView } from "../components/Inbox";
import { StartRun } from "../components/StartRun";
import { useProfile } from "../hooks/useProfile";
import { OTHER_WORKFLOW } from "../lib/review";
import { ReviewPage } from "./ReviewPage";
import { RunsPage } from "./RunsPage";
import { WorkflowPage } from "./WorkflowPage";

const TABS = ["inbox", "review", "runs", "details"] as const;
type Tab = (typeof TABS)[number];

/** One workflow (UI-REFRESH-23): its pipeline (every step as a stage tab, review items
 * first, UI-REFRESH-24), its runs and its details (steps, trust and safety report, start a
 * run), one tab each, the tab kept in `?tab=` (`review` is the pipeline). Items of no known
 * workflow ("Other") have only the pipeline tab. A workflow with a watch trigger also has an
 * Inbox tab before Pipeline (UI-REFRESH-26), and every known workflow has "Start run" next to
 * its title (UI-REFRESH-25). */
export function WorkflowHubPage() {
  const { name = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const other = name === OTHER_WORKFLOW;
  const folders = useWorkflowInbox(other ? null : name);
  const hasInbox = folders.length > 0;
  const asked = params.get("tab");
  const allowed = !other && TABS.includes(asked as Tab) && (asked !== "inbox" || hasInbox);
  const tab: Tab = allowed ? (asked as Tab) : "review";
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
      <Group justify="space-between">
        <Title order={2} data-testid="workflow-title">
          {other ? "Other" : name}
        </Title>
        {!other && (
          <StartRunButton
            name={name}
            onStarted={() => {
              pick("review");
            }}
          />
        )}
      </Group>
      <Tabs value={tab} onChange={pick} keepMounted={false}>
        <Tabs.List data-testid="workflow-tabs">
          {hasInbox && (
            <Tabs.Tab value="inbox" data-testid="inbox-tab">
              Inbox
            </Tabs.Tab>
          )}
          <Tabs.Tab value="review">Pipeline</Tabs.Tab>
          {!other && <Tabs.Tab value="runs">Runs</Tabs.Tab>}
          {!other && <Tabs.Tab value="details">Details</Tabs.Tab>}
        </Tabs.List>
        {hasInbox && (
          <Tabs.Panel value="inbox" pt="sm">
            <Stack data-testid="workflow-inbox">
              {folders.map((f) => (
                <InboxFolderView key={f.folder} folder={f} split />
              ))}
            </Stack>
          </Tabs.Panel>
        )}
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

/** The profile's inbox listing (DASHBOARD-1), only this workflow's watched folders. Shares
 * the dashboard's query, which the app shell keeps current from events. */
function useWorkflowInbox(name: string | null) {
  const { profile } = useProfile();
  const inbox = useQuery({
    queryKey: ["inbox", profile],
    enabled: profile !== null && name !== null,
    queryFn: async () =>
      ok(
        await api.GET("/api/profiles/{profile}/inbox", {
          params: { path: { profile: profile ?? "" } },
        }),
      ),
  });
  return (inbox.data ?? []).filter((f) => f.workflow === name);
}

/** "Start run" in the page header (UI-REFRESH-25): the Details tab's form in a dialog. */
function StartRunButton({ name, onStarted }: { name: string; onStarted: () => void }) {
  const { profile } = useProfile();
  const [open, setOpen] = useState(false);
  const detail = useQuery({
    queryKey: ["workflow", profile, name],
    enabled: profile !== null,
    queryFn: async () =>
      ok(
        await api.GET("/api/workflows/{name}", {
          params: { path: { name }, query: { profile: profile ?? "" } },
        }),
      ),
  });
  return (
    <>
      <Button
        size="sm"
        data-testid="start-run-open"
        onClick={() => {
          setOpen(true);
        }}
      >
        Start run…
      </Button>
      <Modal
        opened={open}
        onClose={() => {
          setOpen(false);
        }}
        title={`Start ${name}`}
      >
        {detail.error && <Alert color="red">{detail.error.message}</Alert>}
        {detail.data === undefined || profile === null ? (
          !detail.error && <Text c="dimmed">Loading…</Text>
        ) : (
          <StartRun
            wf={detail.data}
            profile={profile}
            framed={false}
            onStarted={() => {
              setOpen(false);
              onStarted();
            }}
          />
        )}
      </Modal>
    </>
  );
}
