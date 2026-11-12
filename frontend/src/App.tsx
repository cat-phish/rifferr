import { AppShell, Group, NavLink, Select, Title } from "@mantine/core";
import { useSyncExternalStore } from "react";
import { Link, Route, Routes, useLocation } from "react-router";
import { auth } from "./api/auth";
import { useProfile, withProfile } from "./hooks/useProfile";
import { RunPage } from "./pages/RunPage";
import { RunsPage } from "./pages/RunsPage";
import { ScriptPage } from "./pages/ScriptPage";
import { ScriptsPage } from "./pages/ScriptsPage";
import { SignedOut } from "./pages/SignedOut";
import { WorkflowPage } from "./pages/WorkflowPage";
import { WorkflowsPage } from "./pages/WorkflowsPage";

/** Routes (F-UI-SHELL Interfaces). `/` is Runs in M0; the M2 dashboard takes it later. */
export function App() {
  const signedOut = useSyncExternalStore(auth.subscribe, auth.isSignedOut);
  if (signedOut) return <SignedOut />;
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<RunsPage />} />
        <Route path="/runs" element={<RunsPage />} />
        <Route path="/runs/:id" element={<RunPage />} />
        <Route path="/workflows" element={<WorkflowsPage />} />
        <Route path="/workflows/:name" element={<WorkflowPage />} />
        <Route path="/scripts" element={<ScriptsPage />} />
        <Route path="/scripts/:name" element={<ScriptPage />} />
      </Routes>
    </Shell>
  );
}

/** Profile switcher, navigation and the page area (UI-SHELL-2). */
function Shell({ children }: { children: React.ReactNode }) {
  const { profile, profiles, setProfile } = useProfile();
  const { pathname } = useLocation();
  const nav = [
    { label: "Runs", to: "/runs", active: pathname === "/" || pathname.startsWith("/runs") },
    { label: "Workflows", to: "/workflows", active: pathname.startsWith("/workflows") },
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
            active={n.active}
          />
        ))}
      </AppShell.Navbar>
      <AppShell.Main>{children}</AppShell.Main>
    </AppShell>
  );
}
