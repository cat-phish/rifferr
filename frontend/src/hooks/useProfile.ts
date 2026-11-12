import { useQuery } from "@tanstack/react-query";
import { useCallback } from "react";
import { useSearchParams } from "react-router";
import { api, ok } from "../api/client";
import type { ProfileSummary } from "../api/types";

export function useProfiles() {
  return useQuery({
    queryKey: ["profiles"],
    queryFn: async () => ok(await api.GET("/api/profiles")),
  });
}

/** The selected profile lives in ``?profile=`` (UI-SHELL-2); default: first loadable one. */
export function useProfile(): {
  profile: string | null;
  profiles: ProfileSummary[];
  setProfile: (name: string) => void;
} {
  const [params, setParams] = useSearchParams();
  const { data = [] } = useProfiles();
  const wanted = params.get("profile");
  const loadable = data.filter((p) => p.error === null);
  const profile =
    wanted !== null && loadable.some((p) => p.name === wanted)
      ? wanted
      : (loadable[0]?.name ?? null);
  const setProfile = useCallback(
    (name: string) => {
      setParams((prev) => {
        const next = new URLSearchParams(prev);
        next.set("profile", name);
        return next;
      });
    },
    [setParams],
  );
  return { profile, profiles: data, setProfile };
}

/** Append the current profile to an app link. */
export function withProfile(path: string, profile: string | null): string {
  return profile === null ? path : `${path}?profile=${encodeURIComponent(profile)}`;
}
