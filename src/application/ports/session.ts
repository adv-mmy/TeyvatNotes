import type { ScopeDraft, ScopeField } from "../../domain/query-scope.js";
import type { JsonObject } from "./agent.js";

export interface CitationSnapshot {
	readonly source: string;
	readonly url?: string;
	readonly version: string;
	readonly documentVersionId?: string;
	readonly location: string;
	readonly excerpt: string;
}

export type TurnOutcome =
	| {
			readonly status: "answered";
			readonly text: string;
			readonly citations: readonly CitationSnapshot[];
	  }
	| {
			readonly status: "needs_clarification";
			readonly text: string;
	  }
	| {
			readonly status: "insufficient_evidence";
			readonly text: string;
			readonly supplementGap?: import("../../domain/answer-validation.js").SupplementGap;
	  }
	| { readonly status: "failed"; readonly code: string }
	| { readonly status: "cancelled" | "interrupted" };

export interface CommittedTurn {
	readonly turnId: string;
	readonly prompt: string;
	readonly outcome: TurnOutcome;
}

export interface SessionHistory {
	readonly sessionId: string;
	readonly conditions: JsonObject;
	readonly turns: readonly CommittedTurn[];
	readonly clarification?: PendingClarification | null;
}

export interface PendingClarification {
	readonly questionTurnId: string;
	readonly prompt: string;
	readonly state: "waiting" | "ready";
	readonly text: string;
	readonly scope?: ScopeDraft;
	readonly fields?: readonly ScopeField[];
	readonly equipmentId?: string;
}

export interface ConfirmedConditionUpdate {
	readonly turnId: string;
	readonly conditions: JsonObject;
	readonly clarification?: PendingClarification;
}

export interface VerifiedTurn {
	readonly turnId: string;
	readonly outcome: TurnOutcome;
	readonly clarification?: PendingClarification | null;
}

export interface WritableSession {
	readonly sessionId: string;
	history(): SessionHistory;
	begin(prompt: string): Promise<string>;
	appendConfirmedConditions(change: ConfirmedConditionUpdate): Promise<void>;
	abandonClarification(questionTurnId: string): Promise<void>;
	commit(result: VerifiedTurn): Promise<CommittedTurn>;
	close(): Promise<void>;
}

export type OpenSession =
	| { readonly mode: "write"; readonly session: WritableSession }
	| {
			readonly mode: "read";
			readonly sessionId: string;
			readonly code: "SESSION_BUSY" | "SESSION_CORRUPT";
			readonly history?: SessionHistory;
	  };

export interface SessionStore {
	create(): Promise<WritableSession>;
	open(sessionId: string): Promise<OpenSession>;
	readHistory(sessionId: string): Promise<SessionHistory>;
}

export class SessionError extends Error {
	constructor(
		readonly code:
			| "SESSION_ID_INVALID"
			| "SESSION_NOT_FOUND"
			| "SESSION_FAILED"
			| "SESSION_CORRUPT",
	) {
		super(code);
	}
}
