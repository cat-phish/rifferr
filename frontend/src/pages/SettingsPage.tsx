import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Card,
  Code,
  Group,
  List,
  NumberInput,
  PasswordInput,
  Select,
  Stack,
  Switch,
  Table,
  Tabs,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ComponentType, useState } from "react";
import { ApiError, api, ok } from "../api/client";
import type { components } from "../api/schema";
import { useProfile } from "../hooks/useProfile";

type S = components["schemas"];
type View = S["SettingsView"];
type Values = Record<string, unknown>;
type FieldErrors = Record<string, string>;

/** Section editors by id (SETTINGS-1): a later section is one more entry. */
const SECTION_VIEWS: Record<string, ComponentType<SectionProps>> = { ai: AISection };

interface SectionProps {
  profile: string;
  section: string;
}

/** Settings for the profile (F-SETTINGS SETTINGS-1): one tab per section. */
export function SettingsPage() {
  const { profile } = useProfile();
  const sections = useQuery({
    queryKey: ["settings", profile],
    enabled: profile !== null,
    queryFn: async () =>
      ok(
        await api.GET("/api/profiles/{profile}/settings", {
          params: { path: { profile: profile ?? "" } },
        }),
      ),
  });
  const [tab, setTab] = useState<string | null>(null);
  if (profile === null) return null;
  const list = sections.data ?? [];
  const current = tab ?? list[0]?.id ?? null;
  return (
    <Stack>
      <Title order={2}>Settings</Title>
      <Text size="sm" c="dimmed">
        Saved in this profile&rsquo;s <Code>profile.yaml</Code>; comments and other settings there
        are kept. Runs that start after saving use the new settings.
      </Text>
      {sections.error && <Alert color="red">{sections.error.message}</Alert>}
      {current !== null && (
        <Tabs value={current} onChange={setTab} keepMounted={false}>
          <Tabs.List>
            {list.map((s) => (
              <Tabs.Tab key={s.id} value={s.id}>
                {s.title}
              </Tabs.Tab>
            ))}
          </Tabs.List>
          {list.map((s) => {
            const Editor = SECTION_VIEWS[s.id];
            return (
              <Tabs.Panel key={s.id} value={s.id} pt="md">
                {Editor ? (
                  <Editor profile={profile} section={s.id} />
                ) : (
                  <Text size="sm">This section can&rsquo;t be edited here yet.</Text>
                )}
              </Tabs.Panel>
            );
          })}
        </Tabs>
      )}
    </Stack>
  );
}

function useSection(profile: string, section: string) {
  return useQuery({
    queryKey: ["settings", profile, section],
    queryFn: async () =>
      ok(
        await api.GET("/api/profiles/{profile}/settings/{section}", {
          params: { path: { profile, section } },
        }),
      ),
  });
}

function fieldErrors(err: unknown): FieldErrors {
  if (!(err instanceof ApiError) || !Array.isArray(err.details)) return {};
  const out: FieldErrors = {};
  for (const d of err.details as { field?: string; message?: string }[]) {
    if (d.field) out[d.field] = d.message ?? "invalid";
  }
  return out;
}

/** The top-level settings that differ between two versions, for the conflict notice. */
function changedKeys(a: Values, b: Values): string[] {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  return [...keys].filter((k) => JSON.stringify(a[k]) !== JSON.stringify(b[k])).sort();
}

type NewHost = { host: string; providers: string[] };

/** The hosts a `409 confirm_hosts` asks the user to confirm (SETTINGS-10). */
function newHosts(err: unknown): NewHost[] {
  if (!(err instanceof ApiError) || err.code !== "confirm_hosts") return [];
  return Array.isArray(err.details) ? (err.details as NewHost[]) : [];
}

/** States: loading, clean, edited (Save / Discard), saving, conflict, confirm new hosts,
 * error per field. */
function useEditor(profile: string, section: string) {
  const queryClient = useQueryClient();
  const loaded = useSection(profile, section);
  const [draft, setDraft] = useState<Values | null>(null);
  const [conflict, setConflict] = useState<View | null>(null);
  const saved = loaded.data;
  const values: Values | null = draft ?? saved?.values ?? null;
  const edited = draft !== null && JSON.stringify(draft) !== JSON.stringify(saved?.values);
  const save = useMutation({
    mutationFn: async ({ base, confirm = [] }: { base: string; confirm?: string[] }) =>
      ok(
        await api.PUT("/api/profiles/{profile}/settings/{section}", {
          params: { path: { profile, section } },
          body: {
            values: (draft ?? {}) as Record<string, never>,
            base_hash: base,
            confirm_hosts: confirm,
          },
        }),
      ),
    onSuccess: (view) => {
      queryClient.setQueryData(["settings", profile, section], view);
      setDraft(null);
      setConflict(null);
    },
    onError: async (err) => {
      if (err instanceof ApiError && err.status === 409 && err.code === "conflict") {
        const fresh = await api.GET("/api/profiles/{profile}/settings/{section}", {
          params: { path: { profile, section } },
        });
        if (fresh.data) setConflict(fresh.data);
      }
    },
  });
  return {
    loaded,
    saved,
    values,
    edited,
    conflict,
    save,
    errors: fieldErrors(save.error),
    newHosts: newHosts(save.error),
    set: (next: Values) => {
      setDraft(next);
    },
    discard: () => {
      setDraft(null);
      setConflict(null);
      save.reset();
    },
    reload: () => {
      setDraft(null);
      setConflict(null);
      save.reset();
      void queryClient.invalidateQueries({ queryKey: ["settings", profile, section] });
    },
  };
}

type Editor = ReturnType<typeof useEditor>;

function SaveBar({ ed }: { ed: Editor }) {
  const saved = ed.saved;
  if (!saved) return null;
  const err = ed.save.error;
  return (
    <Stack gap="xs">
      {ed.conflict && (
        <Alert color="yellow" data-testid="settings-conflict" title="Changed on disk">
          <Text size="sm">
            profile.yaml changed since this page loaded it (
            {changedKeys(saved.values, ed.conflict.values).join(", ") || "other settings"}
            ). Reload to see it, or overwrite it with your edits.
          </Text>
          <Group gap="xs" mt="xs">
            <Button size="xs" variant="default" onClick={ed.reload}>
              Reload
            </Button>
            <Button
              size="xs"
              color="yellow"
              onClick={() => {
                if (ed.conflict) ed.save.mutate({ base: ed.conflict.base_hash });
              }}
            >
              Overwrite with my edits
            </Button>
          </Group>
        </Alert>
      )}
      {ed.newHosts.length > 0 && (
        <Alert
          color="orange"
          data-testid="settings-confirm-hosts"
          title="Album data goes to a new host"
        >
          <Text size="sm">
            With these settings, AI requests send album data off this machine to:
          </Text>
          <List size="sm" mt={4}>
            {ed.newHosts.map((h) => (
              <List.Item key={h.host}>
                <Code>{h.host}</Code> ({h.providers.join(", ")})
              </List.Item>
            ))}
          </List>
          <Group gap="xs" mt="xs">
            <Button
              size="xs"
              variant="default"
              onClick={() => {
                ed.save.reset();
              }}
            >
              Cancel
            </Button>
            <Button
              size="xs"
              color="orange"
              loading={ed.save.isPending}
              onClick={() => {
                ed.save.mutate({ base: saved.base_hash, confirm: ed.newHosts.map((h) => h.host) });
              }}
            >
              Send album data to these hosts
            </Button>
          </Group>
        </Alert>
      )}
      {err && !ed.conflict && ed.newHosts.length === 0 && (
        <Alert color="red" data-testid="settings-error">
          {err.message}
        </Alert>
      )}
      <Group gap="xs" justify="flex-end">
        {ed.edited && (
          <Text size="xs" c="dimmed">
            Unsaved changes
          </Text>
        )}
        <Button size="xs" variant="default" disabled={!ed.edited} onClick={ed.discard}>
          Discard
        </Button>
        <Button
          size="xs"
          disabled={!ed.edited}
          loading={ed.save.isPending}
          onClick={() => {
            ed.save.mutate({ base: saved.base_hash });
          }}
        >
          Save
        </Button>
      </Group>
    </Stack>
  );
}

// -- AI (SETTINGS-5 to -7) -----------------------------------------------------------

type Provider = {
  name: string;
  type: string;
  model: string;
  base_url?: string | null;
  api_key?: string | null;
  enabled?: boolean;
  settings?: Values & { price_per_mtok?: { input: number; output: number } | null };
};
type Suggest = { mode?: string; steps?: Record<string, string> };

const MODES = [
  { value: "off", label: "Off" },
  { value: "unsure", label: "When the rules are unsure" },
  { value: "always", label: "Always" },
];

function AISection({ profile, section }: SectionProps) {
  const ed = useEditor(profile, section);
  const values = ed.values;
  const info = ed.saved?.ai;
  if (ed.loaded.error) return <Alert color="red">{ed.loaded.error.message}</Alert>;
  if (!values || !info) return <Text size="sm">Loading…</Text>;
  const providers = (values.providers ?? []) as Provider[];
  const suggest = (values.suggest ?? {}) as Suggest;
  const e = ed.errors;
  const put = (key: string, v: unknown) => {
    ed.set({ ...values, [key]: v });
  };
  const setProviders = (next: Provider[]) => {
    put("providers", next);
  };
  const text = (key: string, label: string, description?: string) => (
    <TextInput
      label={label}
      description={description}
      value={typeof values[key] === "string" ? values[key] : ""}
      error={e[key]}
      onChange={(ev) => {
        put(key, ev.currentTarget.value === "" ? null : ev.currentTarget.value);
      }}
    />
  );
  return (
    <Stack data-testid="settings-ai">
      <Card withBorder>
        <Stack gap="sm">
          <Title order={4}>Providers</Title>
          <Text size="sm" c="dimmed">
            In order; the first enabled one is the default. A step can name another one.
          </Text>
          {providers.map((p, i) => (
            <ProviderCard
              key={i}
              profile={profile}
              provider={p}
              index={i}
              count={providers.length}
              types={info.provider_types}
              status={info.providers.find((s) => s.name === p.name) ?? null}
              keyring={info.keyring}
              errors={e}
              dirty={ed.edited}
              onChange={(next) => {
                setProviders(providers.map((x, j) => (j === i ? next : x)));
              }}
              onMove={(by) => {
                const next = [...providers];
                const [moved] = next.splice(i, 1);
                if (moved) next.splice(i + by, 0, moved);
                setProviders(next);
              }}
              onRemove={() => {
                setProviders(providers.filter((_, j) => j !== i));
              }}
            />
          ))}
          <Group>
            <Button
              size="xs"
              variant="light"
              onClick={() => {
                setProviders([
                  ...providers,
                  { name: "", type: info.provider_types[0] ?? "", model: "", enabled: true },
                ]);
              }}
            >
              Add provider
            </Button>
          </Group>
        </Stack>
      </Card>
      <Card withBorder>
        <Stack gap="sm">
          <Title order={4}>AI suggestions on choices</Title>
          <Select
            label="Ask the AI on its own"
            description="For tags.choose steps with an ai block. Ask AI on a card always works."
            data={MODES}
            value={suggest.mode ?? "unsure"}
            error={e["suggest.mode"]}
            onChange={(v) => {
              put("suggest", { ...suggest, mode: v ?? "unsure" });
            }}
            allowDeselect={false}
          />
          <SuggestOverrides
            steps={info.steps}
            overrides={suggest.steps ?? {}}
            onChange={(next) => {
              put("suggest", { ...suggest, steps: next });
            }}
          />
        </Stack>
      </Card>
      <Card withBorder>
        <Stack gap="sm">
          <Title order={4}>Requests</Title>
          <NumberInput
            label="Requests at the same time"
            min={1}
            value={Number(values.concurrency ?? 2)}
            error={e.concurrency}
            onChange={(v) => {
              put("concurrency", typeof v === "number" ? v : Number(v));
            }}
          />
          {text("timeout", "Timeout", "e.g. 60s")}
          {text(
            "cache_ttl",
            "Keep answers for",
            "e.g. 90d; asking again with the same input uses it",
          )}
          {text(
            "lookalikes",
            "Lookalike characters file",
            "Relative to the profile folder, e.g. rules/lookalikes.yaml",
          )}
        </Stack>
      </Card>
      <SaveBar ed={ed} />
    </Stack>
  );
}

function SuggestOverrides({
  steps,
  overrides,
  onChange,
}: {
  steps: S["AISuggestStep"][];
  overrides: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
}) {
  if (steps.length === 0)
    return (
      <Text size="sm" c="dimmed">
        No workflow step in this profile has an ai block.
      </Text>
    );
  return (
    <Table data-testid="suggest-overrides" withRowBorders={false} verticalSpacing={4}>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Step</Table.Th>
          <Table.Th>The step&rsquo;s own mode</Table.Th>
          <Table.Th>Override</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {steps.map((s) => (
          <Table.Tr key={s.ref} data-testid="suggest-override">
            <Table.Td ff="monospace">{s.ref}</Table.Td>
            <Table.Td>
              <Text size="sm" c="dimmed">
                {s.mode
                  ? (MODES.find((m) => m.value === s.mode)?.label ?? s.mode)
                  : "none (uses the profile mode)"}
              </Text>
            </Table.Td>
            <Table.Td>
              <Select
                aria-label={`Override for ${s.ref}`}
                size="xs"
                data={[{ value: "", label: "No override" }, ...MODES]}
                value={overrides[s.ref] ?? ""}
                allowDeselect={false}
                onChange={(v) => {
                  const next = Object.fromEntries(
                    Object.entries(overrides).filter(([k]) => k !== s.ref),
                  );
                  if (v) next[s.ref] = v;
                  onChange(next);
                }}
              />
            </Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
}

function ProviderCard({
  profile,
  provider: p,
  index,
  count,
  types,
  status,
  keyring,
  errors,
  dirty,
  onChange,
  onMove,
  onRemove,
}: {
  profile: string;
  provider: Provider;
  index: number;
  count: number;
  types: string[];
  status: S["ProviderStatus"] | null;
  keyring: boolean;
  errors: FieldErrors;
  dirty: boolean;
  onChange: (next: Provider) => void;
  onMove: (by: number) => void;
  onRemove: () => void;
}) {
  const at = (f: string) => errors[`providers.${String(index)}.${f}`];
  const price = p.settings?.price_per_mtok ?? null;
  const setPrice = (key: "input" | "output", v: number | string) => {
    const n = typeof v === "number" ? v : v === "" ? null : Number(v);
    const other = key === "input" ? price?.output : price?.input;
    const next =
      n === null && other == null
        ? null
        : { input: price?.input ?? 0, output: price?.output ?? 0, [key]: n ?? 0 };
    const settings = { ...(p.settings ?? {}) };
    if (next === null) delete settings.price_per_mtok;
    else settings.price_per_mtok = next;
    onChange({ ...p, settings });
  };
  return (
    <Card withBorder padding="sm" data-testid="provider" data-name={p.name}>
      <Stack gap="xs">
        <Group justify="space-between">
          <Group gap="xs">
            <Text fw={500}>{p.name || "New provider"}</Text>
            {index === 0 && p.enabled !== false && (
              <Badge size="xs" variant="light">
                default
              </Badge>
            )}
          </Group>
          <Group gap={4}>
            <ActionIcon
              variant="subtle"
              aria-label={`Move ${p.name} up`}
              disabled={index === 0}
              onClick={() => {
                onMove(-1);
              }}
            >
              ↑
            </ActionIcon>
            <ActionIcon
              variant="subtle"
              aria-label={`Move ${p.name} down`}
              disabled={index === count - 1}
              onClick={() => {
                onMove(1);
              }}
            >
              ↓
            </ActionIcon>
            <Button size="compact-xs" variant="subtle" color="red" onClick={onRemove}>
              Remove
            </Button>
          </Group>
        </Group>
        <Group grow align="flex-start">
          <TextInput
            label="Name"
            value={p.name}
            error={at("name")}
            onChange={(ev) => {
              onChange({ ...p, name: ev.currentTarget.value });
            }}
          />
          <Select
            label="Type"
            data={types}
            value={p.type}
            error={at("type")}
            allowDeselect={false}
            onChange={(v) => {
              onChange({ ...p, type: v ?? p.type });
            }}
          />
          <TextInput
            label="Model"
            value={p.model}
            error={at("model")}
            onChange={(ev) => {
              onChange({ ...p, model: ev.currentTarget.value });
            }}
          />
        </Group>
        <TextInput
          label="Server URL"
          description="Empty: the provider's own. http only for hosts on this machine or network."
          value={p.base_url ?? ""}
          error={at("base_url")}
          onChange={(ev) => {
            onChange({ ...p, base_url: ev.currentTarget.value || null });
          }}
        />
        <Group grow align="flex-start">
          <NumberInput
            label="Price per million input tokens"
            description="Optional, for cost estimates"
            min={0}
            decimalScale={6}
            value={price?.input ?? ""}
            onChange={(v) => {
              setPrice("input", v);
            }}
          />
          <NumberInput
            label="Price per million output tokens"
            description="Optional"
            min={0}
            decimalScale={6}
            value={price?.output ?? ""}
            onChange={(v) => {
              setPrice("output", v);
            }}
          />
        </Group>
        <Switch
          label="Enabled"
          checked={p.enabled !== false}
          onChange={(ev) => {
            onChange({ ...p, enabled: ev.currentTarget.checked });
          }}
        />
        {status && !status.local && (
          <Text size="xs" c="dimmed" data-testid="remote-notice">
            Requests send album data to {status.host ?? "the provider"}, off this machine.
          </Text>
        )}
        {status ? (
          <ProviderKey
            profile={profile}
            name={p.name}
            status={status}
            keyring={keyring}
            dirty={dirty}
          />
        ) : (
          <Text size="xs" c="dimmed">
            Save to set this provider&rsquo;s key and test it.
          </Text>
        )}
      </Stack>
    </Card>
  );
}

/** Write-only key and Test connection (SETTINGS-6, -7). */
function ProviderKey({
  profile,
  name,
  status,
  keyring,
  dirty,
}: {
  profile: string;
  name: string;
  status: S["ProviderStatus"];
  keyring: boolean;
  dirty: boolean;
}) {
  const queryClient = useQueryClient();
  const [key, setKey] = useState("");
  const path = { params: { path: { profile, provider: name } } };
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["settings", profile] });
  };
  const store = useMutation({
    mutationFn: async () =>
      ok(
        await api.PUT("/api/profiles/{profile}/settings/ai/providers/{provider}/key", {
          ...path,
          body: { key },
        }),
      ),
    onSuccess: () => {
      setKey("");
      refresh();
    },
  });
  const clear = useMutation({
    mutationFn: async () =>
      ok(
        await api.DELETE("/api/profiles/{profile}/settings/ai/providers/{provider}/key", {
          ...path,
          body: {},
        }),
      ),
    onSuccess: refresh,
  });
  const test = useMutation({
    mutationFn: async () =>
      ok(await api.POST("/api/profiles/{profile}/settings/ai/providers/{provider}/test", path)),
  });
  const keyErr = store.error instanceof ApiError ? store.error : null;
  const noKeyring = keyErr?.code === "no_keyring";
  const hint = noKeyring
    ? (keyErr.details as { env_var?: string; env_line?: string } | undefined)
    : undefined;
  return (
    <Stack gap="xs" data-testid="provider-key">
      <Group gap="xs" align="flex-end">
        <Text size="sm" data-testid="key-status">
          {status.key_set ? `Key set (${status.key_secret ?? ""})` : "No key set"}
        </Text>
        {status.key_set && (
          <Button
            size="compact-xs"
            variant="subtle"
            color="red"
            loading={clear.isPending}
            onClick={() => {
              clear.mutate();
            }}
          >
            Clear key
          </Button>
        )}
      </Group>
      {keyring ? (
        <Group gap="xs" align="flex-end">
          <PasswordInput
            label={status.key_set ? "Replace the key" : "API key"}
            description="Stored in the system keyring; never shown again"
            value={key}
            autoComplete="off"
            onChange={(ev) => {
              setKey(ev.currentTarget.value);
            }}
            style={{ flex: 1 }}
          />
          <Button
            size="xs"
            disabled={key === "" || dirty}
            loading={store.isPending}
            onClick={() => {
              store.mutate();
            }}
          >
            Save key
          </Button>
        </Group>
      ) : (
        <Alert color="gray" data-testid="no-keyring">
          No system keyring here (e.g. in Docker). Set the key as an environment variable and name
          it in profile.yaml, e.g.{" "}
          <Code>
            RIFFERR_{"<PROFILE>"}_{"<PROVIDER>"}_KEY
          </Code>{" "}
          and <Code>secrets: {"{ai-<provider>: env:RIFFERR_…_KEY}"}</Code>, then set the
          provider&rsquo;s api_key to <Code>ai-{name}</Code>.
        </Alert>
      )}
      {hint && (
        <Alert color="gray">
          Set <Code>{hint.env_var}</Code> and add <Code>{hint.env_line}</Code>.
        </Alert>
      )}
      {store.error && !noKeyring && <Alert color="red">{store.error.message}</Alert>}
      {dirty && (
        <Text size="xs" c="dimmed">
          Save or discard your edits before changing the key.
        </Text>
      )}
      <Group gap="xs">
        <Button
          size="xs"
          variant="default"
          loading={test.isPending}
          disabled={dirty}
          onClick={() => {
            test.mutate();
          }}
        >
          Test connection
        </Button>
        {test.data && (
          <Text size="sm" c={test.data.ok ? "green" : "red"} data-testid="test-result">
            {test.data.ok
              ? `Connected: ${test.data.model ?? ""}`
              : `Failed: ${test.data.error ?? ""}`}
          </Text>
        )}
        {test.error && (
          <Text size="sm" c="red">
            {test.error.message}
          </Text>
        )}
      </Group>
    </Stack>
  );
}
