import { ActionIcon, AppShell, Badge, Group, NavLink, Select, Title } from "@mantine/core";
import { useState, useSyncExternalStore } from "react";
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
  const [reviewOpen, setReviewOpen] = useStoredFlag("rifferr.nav.review-open", true);
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
        {nav.map((n) => {
          const sub = n.to === "/review" && workflows.length > 0;
          return (
            <NavLink
              key={n.to}
              component={Link}
              to={withProfile(n.to, profile)}
              label={n.label}
              active={n.active && (!sub || openWorkflow === null)}
              opened={sub && reviewOpen}
              // The count badge is not a chevron: it must not turn (Mantine turns a
              // NavLink's right section when it has children).
              disableRightSectionRotation
              rightSection={
                n.count !== undefined && (n.count > 0 || sub) ? (
                  <Group gap={4} wrap="nowrap">
                    {n.count > 0 && (
                      <Badge size="sm" circle data-testid="review-count">
                        {n.count}
                      </Badge>
                    )}
                    {sub && (
                      <ActionIcon
                        size="sm"
                        variant="subtle"
                        color="gray"
                        aria-label={reviewOpen ? "Hide workflows" : "Show workflows"}
                        data-testid="review-toggle"
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          setReviewOpen(!reviewOpen);
                        }}
                      >
                        <span
                          style={{
                            display: "inline-block",
                            transform: reviewOpen ? "rotate(90deg)" : undefined,
                            transition: "transform 150ms",
                          }}
                        >
                          ›
                        </span>
                      </ActionIcon>
                    )}
                  </Group>
                ) : undefined
              }
            >
              {sub
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
          );
        })}
      </AppShell.Navbar>
      <AppShell.Main>{children}</AppShell.Main>
    </AppShell>
  );
}

/** A boolean remembered in this browser (a per-viewer convenience; works without storage). */
function useStoredFlag(key: string, fallback: boolean): [boolean, (v: boolean) => void] {
  const [value, setValue] = useState(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : raw === "1";
    } catch {
      return fallback;
    }
  });
  const set = (v: boolean) => {
    setValue(v);
    try {
      localStorage.setItem(key, v ? "1" : "0");
    } catch {
      // storage blocked: the choice lasts until reload
    }
  };
  return [value, set];
}
