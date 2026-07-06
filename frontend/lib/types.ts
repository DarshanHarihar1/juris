// Event log rows (LLD §3.2) streamed from Supabase Realtime, and the VerdictCard (§4.5).

export type EventName =
  | "stage"
  | "claim"
  | "evidence"
  | "vote"
  | "escalation"
  | "argument"
  | "ruling"
  | "verdict"
  | "terminal";

export interface EventRow {
  id: number;
  job_id: string;
  event: EventName;
  data: Record<string, any>;
  created_at: string;
}

export type VerdictClass =
  | "TRUE"
  | "FALSE"
  | "MISLEADING"
  | "UNVERIFIABLE"
  | "CONFLICTING";

export interface EvidenceRef {
  url: string;
  domain: string;
  stance?: string | null;
  date?: string | null;
}

export interface VerdictCard {
  slug: string;
  claim_native: string;
  claim_en: string;
  verdict: VerdictClass;
  confidence: number;
  one_liner_native: string;
  explanation_native: string;
  manipulation_tags: string[];
  evidence: EvidenceRef[];
  rebuttal_card_native: string;
  path: "cache" | "precedent" | "consensus" | "trial";
  models_used: Record<string, string>;
}

export const STAGES = [
  "S0_INTAKE",
  "S1_NORMALIZE",
  "S2_PRECEDENT",
  "S3_INVESTIGATE",
  "S4_FASTPATH",
  "S5_TRIAL",
  "S6_SYNTHESIZE",
] as const;

export const STAGE_LABEL: Record<string, string> = {
  S0_INTAKE: "Intake",
  S1_NORMALIZE: "Normalize",
  S2_PRECEDENT: "Precedent",
  S3_INVESTIGATE: "Investigate",
  S4_FASTPATH: "Jury",
  S5_TRIAL: "Trial",
  S6_SYNTHESIZE: "Verdict",
};

export const VERDICT_COLOR: Record<VerdictClass, string> = {
  TRUE: "text-verdict-true",
  FALSE: "text-verdict-false",
  MISLEADING: "text-verdict-misleading",
  UNVERIFIABLE: "text-verdict-unverifiable",
  CONFLICTING: "text-verdict-conflicting",
};

export const VERDICT_DOT: Record<VerdictClass, string> = {
  TRUE: "bg-verdict-true",
  FALSE: "bg-verdict-false",
  MISLEADING: "bg-verdict-misleading",
  UNVERIFIABLE: "bg-verdict-unverifiable",
  CONFLICTING: "bg-verdict-conflicting",
};
