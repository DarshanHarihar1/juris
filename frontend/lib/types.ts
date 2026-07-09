// Event log rows streamed from Supabase Realtime, and the verdict card shown on permalinks.

export type EventName =
  | "stage"
  | "claim"
  | "evidence"
  | "verify_step"
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
  | "MOSTLY_TRUE"
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
  path: "verify";
  models_used: Record<string, string>;
}

export const STAGES = [
  "NORMALIZE",
  "VERIFY",
  "SYNTHESIZE",
] as const;

export const STAGE_LABEL: Record<string, string> = {
  NORMALIZE: "Normalize",
  VERIFY: "Verify",
  SYNTHESIZE: "Synthesize",
};

export const VERDICT_COLOR: Record<VerdictClass, string> = {
  TRUE: "text-verdict-true",
  FALSE: "text-verdict-false",
  MOSTLY_TRUE: "text-verdict-true",
  MISLEADING: "text-verdict-misleading",
  UNVERIFIABLE: "text-verdict-unverifiable",
  CONFLICTING: "text-verdict-conflicting",
};

export const VERDICT_DOT: Record<VerdictClass, string> = {
  TRUE: "bg-verdict-true",
  FALSE: "bg-verdict-false",
  MOSTLY_TRUE: "bg-verdict-true",
  MISLEADING: "bg-verdict-misleading",
  UNVERIFIABLE: "bg-verdict-unverifiable",
  CONFLICTING: "bg-verdict-conflicting",
};
