/**
 * Child answer payload types — exact shapes the grader reads in Phase 2.
 * These mirror the contract in the task brief and MUST NOT deviate.
 * Never import from web/src/api/ for payload shapes — the API types.gen only
 * carries `payload: { [key: string]: unknown }`.
 */

export interface McqPayload {
  selected_index: number;
}

export interface TrueFalsePayload {
  value: boolean;
}

export interface MatchingPair {
  left: number;
  right: number;
}
export interface MatchingPayload {
  pairs: MatchingPair[];
}

export interface OrderingPayload {
  order: number[];
}

export interface FillBlankPayload {
  values: string[];
}

export interface ShortAnswerPayload {
  text: string;
}

export interface CalculationPayload {
  answer: string;
  working: string;
}

export interface TableCellEntry {
  row: number;
  col: number;
  value: string;
}
export interface TableCompletionPayload {
  cells: TableCellEntry[];
}

export interface LabelEntry {
  position_id: string;
  term_index: number;
}
export interface LabellingPayload {
  labels: LabelEntry[];
}

export interface ExtendedResponsePayload {
  text: string;
}

export type CapturePayload =
  | McqPayload
  | TrueFalsePayload
  | MatchingPayload
  | OrderingPayload
  | FillBlankPayload
  | ShortAnswerPayload
  | CalculationPayload
  | TableCompletionPayload
  | LabellingPayload
  | ExtendedResponsePayload
  | Record<string, never>; // skipped

export interface ResponseDraft {
  qid: string;
  attempted: boolean;
  payload: CapturePayload;
}
