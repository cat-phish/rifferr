import { AppShell, Badge, Group, NavLink, Select, Title } from "@mantine/core";
import { useSyncExternalStore } from "react";
import { Link, Route, Routes, useLocation } from "react-router";
import { auth } from "./api/auth";
import { useProfile, withProfile } from "./hooks/useProfile";
import { useAppEvents, useReviewQueue, useWorkflowSteps } from "./hooks/useReviewQueue";
import { reviewWorkflows } from "./lib/review";
import { DashboardPage } from "./pages/DashboardPage";
import { LibraryPage } from "./pages/LibraryPage";
import { ReviewPage } from "./pages/ReviewPage";
import { RunPage } from "./pages/RunPage";
import { RunsPage } from "./pages/RunsPage";
import { ScriptPage } from "./pages/ScriptPage";
import { ScriptsPage } from "./pages/ScriptsPage";
import { SignedOut } from "./pages/SignedOut";
import { WorkflowPage } from "./pages/WorkflowPage";
import { WorkflowsPage } from "./pages/WorkflowsPage";

/** Routes (F-UI-SHELL Interfaces). `/` is the queue dashboard (F-DASHBOARD DASHBOARD-14);
 * `/runs` stays the runs list. */
export function App() {
  const signedOut = useSyncExternalStore(auth.subscribe, auth.isSignedOut);
  if (signedOut) return <SignedOut />;
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/runs" element={<RunsPage />} />
        <Route path="/runs/:id" element={<RunPage />} />
        <Route path="/workflows" element={<WorkflowsPage />} />
        <Route path="/workflows/:name" element={<WorkflowPage />} />
        <Route path="/scripts" element={<ScriptsPage />} />
        <Route path="/scripts/:name" element={<ScriptPage />} />
        <Route path="/library" element={<LibraryPage />} />
        <Route path="/review" element={<ReviewPage />} />
      </Routes>
    </Shell>
  );
}

/** Profile switcher, navigation and the page area (UI-SHELL-2). */
function Shell({ children }: { children: React.ReactNode }) {
  const { profile, profiles, setProfile } = useProfile();
  const { pathname, search } = useLocation();
  const queue = useReviewQueue(profile);
  useAppEvents();
  const waiting = queue.data?.length ?? 0;
  // One entry per workflow under Review, always listed, with its count (UI-REFRESH-22).
  const workflows = reviewWorkflows(useWorkflowSteps(profile), queue.data ?? []);
  const openWorkflow = new URLSearchParams(search).get("workflow");
  const nav: { label: string; to: string; active: boolean; count?: number }[] = [
    { label: "Dashboard", to: "/", active: pathname === "/" },
    { label: "Runs", to: "/runs", active: pathname.startsWith("/runs") },
    {
      label: "Review",
      to: "/review",
      active: pathname.startsWith("/review"),
      count: waiting,
    },
    { label: "Workflows", to: "/workflows", active: pathname.startsWith("/workflows") },
    { label: "Library", to: "/library", active: pathname.startsWith("/library") },
    { label: "Scripts", to: "/scripts", active: pathname.startsWith("/scripts") },
  ];
  return (
    <AppShell header={{ height: 56 }} navbar={{ width: 200, breakpoint: "sm" }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Title order={3}>Rifferr</Title>
          <Select
            aria-label="Profile"
            data-testid="profile"
            placeholder="No profile"
            data={profiles.map((p) => ({
              value: p.name,
              label: p.error === null ? p.name : `${p.name} (can't load)`,
              disabled: p.error !== null,
            }))}
            value={profile}
            onChange={(v) => {
              if (v !== null) setProfile(v);
            }}
            allowDeselect={false}
            w={220}
          />
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs">
        {nav.map((n) => (
          <NavLink
            key={n.to}
            component={Link}
            to={withProfile(n.to, profile)}
            label={n.label}
            active={n.active && (n.to !== "/review" || workflows.length < 2)}
            opened={n.to === "/review" && workflows.length > 1}
            rightSection={
              n.count !== undefined && n.count > 0 ? (
                <Badge size="sm" circle data-testid="review-count">
                  {n.count}
                </Badge>
              ) : undefined
            }
          >
            {n.to === "/review" && workflows.length > 1
              ? workflows.map((w) => (
                  <NavLink
                    key={w.name}
                    component={Link}
                    to={`${withProfile("/review", profile)}${profile === null ? "?" : "&"}workflow=${encodeURIComponent(w.name)}`}
                    label={w.name || "Other"}
                    active={n.active && openWorkflow === w.name}
                    data-testid="review-workflow-link"
                    rightSection={
                      <Badge
                        size="sm"
                        circle
                        variant="light"
                        color={w.count === 0 ? "gray" : undefined}
                        data-testid="review-workflow-count"
                      >
                        {w.count}
                      </Badge>
                    }
                  />
                ))
              : undefined}
          </NavLink>
        ))}
      </AppShell.Navbar>
      <AppShell.Main>{children}</AppShell.Main>
    </AppShell>
  );
}
