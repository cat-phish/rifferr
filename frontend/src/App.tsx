import {
  ActionIcon,
  AppShell,
  Badge,
  Button,
  Group,
  Menu,
  NavLink,
  Select,
  Title,
} from "@mantine/core";
import { useState, useSyncExternalStore } from "react";
import { Link, Route, Routes, useLocation } from "react-router";
import { auth } from "./api/auth";
import { signOut } from "./api/session";
import { useProfile, withProfile } from "./hooks/useProfile";
import { useAppEvents, useReviewQueue, useWorkflowSteps } from "./hooks/useReviewQueue";
import { OTHER_WORKFLOW, reviewWorkflows, workflowLink } from "./lib/review";
import { DashboardPage } from "./pages/DashboardPage";
import { LibraryPage } from "./pages/LibraryPage";
import { ReviewPage } from "./pages/ReviewPage";
import { RunPage } from "./pages/RunPage";
import { RunsPage } from "./pages/RunsPage";
import { ScriptPage } from "./pages/ScriptPage";
import { ScriptsPage } from "./pages/ScriptsPage";
import { SignedOut } from "./pages/SignedOut";
import { WorkflowHubPage } from "./pages/WorkflowHubPage";
import { WorkflowsPage } from "./pages/WorkflowsPage";

/** Routes (F-UI-SHELL Interfaces). `/` is the queue dashboard (F-DASHBOARD DASHBOARD-14);
 * `/runs` stays the runs list. `/workflows` is the hub with each workflow's page and its
 * review items; `/review` links go on there; `/workflow-files` is the list to trust, start
 * and edit workflows (UI-REFRESH-23). */
export function App() {
  const signedOut = useSyncExternalStore(auth.subscribe, auth.isSignedOut);
  if (signedOut) return <SignedOut />;
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/runs" element={<RunsPage />} />
        <Route path="/runs/:id" element={<RunPage />} />
        <Route path="/workflows" element={<ReviewPage hub />} />
        <Route path="/workflows/:name" element={<WorkflowHubPage />} />
        <Route path="/workflow-files" element={<WorkflowsPage />} />
        <Route path="/scripts" element={<ScriptsPage />} />
        <Route path="/scripts/:name" element={<ScriptPage />} />
        <Route path="/library" element={<LibraryPage />} />
        <Route path="/review" element={<ReviewPage />} />
      </Routes>
    </Shell>
  );
}

/** Sign out of this browser, or end every session (SECURITY-16). */
function AccountMenu() {
  const leave = (everywhere: boolean) => {
    void signOut(everywhere).then(() => {
      auth.markSignedOut();
    });
  };
  return (
    <Menu position="bottom-end">
      <Menu.Target>
        <Button variant="subtle" size="compact-sm" data-testid="account-menu">
          Account
        </Button>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Item
          onClick={() => {
            leave(false);
          }}
        >
          Sign out
        </Menu.Item>
        <Menu.Item
          color="red"
          onClick={() => {
            leave(true);
          }}
        >
          Sign out everywhere
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  );
}

/** Profile switcher, navigation and the page area (UI-SHELL-2). */
function Shell({ children }: { children: React.ReactNode }) {
  const { profile, profiles, setProfile } = useProfile();
  const { pathname } = useLocation();
  const queue = useReviewQueue(profile);
  useAppEvents();
  const waiting = queue.data?.length ?? 0;
  // Every workflow under Workflows, always listed, with its count (UI-REFRESH-22, -23).
  const workflows = reviewWorkflows(useWorkflowSteps(profile), queue.data ?? [], true);
  const match = /^\/workflows\/([^/]+)/.exec(pathname);
  const open = match?.[1] === undefined ? null : decodeURIComponent(match[1]);
  const openWorkflow = open === OTHER_WORKFLOW ? "" : open;
  const [reviewOpen, setReviewOpen] = useStoredFlag("rifferr.nav.review-open", true);
  const nav: { label: string; to: string; active: boolean; count?: number }[] = [
    { label: "Dashboard", to: "/", active: pathname === "/" },
    { label: "Runs", to: "/runs", active: pathname.startsWith("/runs") },
    {
      label: "Workflows",
      to: "/workflows",
      active: pathname.startsWith("/workflows") || pathname.startsWith("/workflow-files"),
      count: waiting,
    },
    { label: "Library", to: "/library", active: pathname.startsWith("/library") },
    { label: "Scripts", to: "/scripts", active: pathname.startsWith("/scripts") },
  ];
  return (
    <AppShell header={{ height: 56 }} navbar={{ width: 200, breakpoint: "sm" }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Title order={3}>Rifferr</Title>
          <Group gap="sm">
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
            <AccountMenu />
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs">
        {nav.map((n) => {
          const sub = n.to === "/workflows" && workflows.length > 0;
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
                      to={workflowLink(profile, w.name)}
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
