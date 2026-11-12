import type { components } from "./schema";

type S = components["schemas"];
export type ProfileSummary = S["ProfileSummary"];
export type WorkflowSummary = S["WorkflowSummary"];
export type WorkflowDetail = S["WorkflowDetail"];
export type ValidateResult = S["ValidateResult"];
export type SafetyFinding = S["SafetyFinding"];
export type RunSummary = S["RunSummary"];
export type RunDetail = S["RunDetail"];
export type StepState = S["StepState"];
export type VarInfo = S["VarInfo"];
export type WorkflowError = S["WorkflowError"];
export type ErrorEnvelope = S["ErrorEnvelope"];

/** One event of a run's SSE stream (C-RUN RUN-6). Not in OpenAPI: SSE bodies aren't typed. */
export interface RunEvent {
  seq: number;
  ts: string;
  run_id: string;
  type: string;
  step_id?: string | null;
  data: Record<string, unknown>;
}
