import {
	type AgentRuntime,
	type JsonObject,
	type JsonValue,
	type ToolCall,
	type ToolDefinition,
	type ToolResult,
	untilAborted,
} from "./ports/agent.js";
import type {
	CitationSnapshot,
	CommittedTurn,
	SessionHistory,
	TurnOutcome,
	WritableSession,
} from "./ports/session.js";
import { TurnBudget, type TurnLimits } from "./turn-budget.js";

export interface TurnInput extends DialogueTurn {
	readonly onProgress?: (stage: "model" | "tool") => void;
}
export interface TurnToolObservation {
	readonly call: ToolCall;
	readonly content: JsonValue;
}

export interface TurnTool {
	readonly definition: ToolDefinition;
	/** 本批产生的结果不参与本批授权。 */
	authorize(call: ToolCall, previous: readonly TurnToolObservation[]): boolean;
	execute(
		call: ToolCall,
		signal: AbortSignal,
	): Promise<{
		readonly content: JsonValue;
		readonly evidence?: {
			readonly id: string;
			readonly citation: CitationSnapshot;
		};
		readonly gap?: true;
	}>;
}

function validateAnswer(
	text: string,
	evidence: ReadonlyMap<string, CitationSnapshot>,
	gap: boolean,
): TurnOutcome {
	let decoded: unknown;
	try {
		decoded = JSON.parse(text);
	} catch {
		return { status: "failed", code: "INVALID_ANSWER_STRUCTURE" };
	}
	if (!decoded || typeof decoded !== "object" || Array.isArray(decoded))
		return { status: "failed", code: "INVALID_ANSWER_STRUCTURE" };
	const value = decoded as Record<string, unknown>;
	if (
		typeof value.text !== "string" ||
		!value.text.trim() ||
		Object.keys(value).some(
			(key) => !["status", "text", "evidenceIds"].includes(key),
		)
	)
		return { status: "failed", code: "INVALID_ANSWER_STRUCTURE" };
	if (value.status === "answered" && Array.isArray(value.evidenceIds)) {
		if (
			!value.evidenceIds.length ||
			new Set(value.evidenceIds).size !== value.evidenceIds.length
		)
			return { status: "failed", code: "INVALID_CITATION" };
		const citations: CitationSnapshot[] = [];
		for (const id of value.evidenceIds) {
			const citation = typeof id === "string" ? evidence.get(id) : undefined;
			if (!citation) return { status: "failed", code: "INVALID_CITATION" };
			citations.push(citation);
		}
		return { status: "answered", text: value.text, citations };
	}
	if (value.status === "needs_clarification" && !("evidenceIds" in value))
		return { status: "needs_clarification", text: value.text };
	if (value.status === "insufficient_evidence" && !("evidenceIds" in value)) {
		return gap
			? { status: "insufficient_evidence", text: value.text }
			: { status: "failed", code: "INVALID_ANSWER_STRUCTURE" };
	}
	return { status: "failed", code: "INVALID_ANSWER_STRUCTURE" };
}

function trustedHistory(history: SessionHistory) {
	return history.turns.flatMap(({ prompt, outcome }) =>
		outcome.status === "answered" ||
		outcome.status === "needs_clarification" ||
		outcome.status === "insufficient_evidence"
			? [{ prompt, answer: outcome.text }]
			: [],
	);
}

function scopeContext(scope: QueryScope, turnId: string): JsonObject {
	return { queryScope: { scopeId: turnId, ...scope } };
}

export class RunTurn {
	constructor(
		private readonly runtime: AgentRuntime,
		private readonly limits: TurnLimits,
		private readonly tools: readonly TurnTool[],
		private readonly diagnose: (
			info: ReturnType<TurnBudget["summary"]> & {
				sessionId: string;
				turnId: string;
				status: TurnOutcome["status"];
				errorCode?: string;
				elapsedMs: number;
			},
		) => void = () => {},
		private readonly clarification = new Clarification(),
	) {}

	pendingClarification(session: WritableSession) {
		return this.clarification.current(session);
	}

	discardDraft(session: WritableSession) {
		return this.clarification.discardDraft(session);
	}

	async submit(
		session: WritableSession,
		line: string,
		signal: AbortSignal,
		onProgress: TurnInput["onProgress"],
	) {
		const input = await this.clarification.prepare(session, line);
		if (input.kind === "notice") return input;
		const turn = await this.runTurn(
			session,
			{ ...input, ...(onProgress ? { onProgress } : {}) },
			signal,
		);
		return { kind: "committed", turn } as const;
	}

	async runTurn(
		session: WritableSession,
		input: TurnInput,
		signal: AbortSignal,
	): Promise<CommittedTurn> {
		const budget = new TurnBudget(this.limits, signal);
		try {
			return await this.execute(session, input, signal, budget);
		} finally {
			budget.close();
		}
	}

	private async execute(
		session: WritableSession,
		input: TurnInput,
		signal: AbortSignal,
		budget: TurnBudget,
	): Promise<CommittedTurn> {
		const { prompt, confirmation, preflight, scope, onProgress } = input;
		const started = performance.now();
		const turnId = await session.begin(prompt);
		let outcome: TurnOutcome = { status: "failed", code: "RUNTIME_FAILED" };
		const registered = new Map(
			this.tools.map((tool) => [tool.definition.name, tool]),
		);
		const evidence = new Map<string, CitationSnapshot>();
		const observations: TurnToolObservation[] = [];
		let gap = false;
		let terminal:
			| "BUDGET_EXHAUSTED"
			| "TOOL_TIMEOUT"
			| "TOOL_FAILED"
			| undefined;
		try {
			if (confirmation && !budget.signal.aborted)
				await session.appendConfirmedConditions({
					turnId,
					...confirmation,
				});
			if (preflight) outcome = preflight;
			else if (!budget.signal.aborted) {
				const history = session.history();
				const result = await this.runtime.run(
					{
						prompt,
						tools: this.tools.map((tool) => tool.definition),
						turnId,
						priorTurns: scope ? [] : trustedHistory(history),
						conditions: scope
							? scopeContext(scope, turnId)
							: history.conditions,
					},
					{
						prepareModelRequest: async (draft) => {
							const prepared = budget.prepare(draft);
							if (prepared.allowed) onProgress?.("model");
							return prepared;
						},
						recordModelUsage: (tokens) => {
							if (!budget.recordUsage(tokens)) terminal ??= "BUDGET_EXHAUSTED";
						},
						executeToolBatch: async (batch) => {
							if (terminal) throw new Error(terminal);
							const previous = observations.slice();
							const results: ToolResult[] = [];
							budget.recordRejection(batch.rejected.length);
							for (const call of batch.calls) {
								batch.signal.throwIfAborted();
								const tool = registered.get(call.name);
								if (!tool?.authorize(call, previous)) {
									budget.recordRejection();
									results.push({
										id: call.id,
										ok: false,
										code: "TOOL_NOT_ALLOWED",
									});
									continue;
								}
								const timeout = budget.startTool();
								if (!timeout) {
									terminal = "BUDGET_EXHAUSTED";
									throw new Error(terminal);
								}
								const toolSignal = AbortSignal.any([
									batch.signal,
									budget.signal,
									timeout,
								]);
								onProgress?.("tool");
								const resolved = await untilAborted(
									() => tool.execute(call, toolSignal),
									toolSignal,
								).catch((error: unknown) => {
									terminal = timeout.aborted ? "TOOL_TIMEOUT" : "TOOL_FAILED";
									throw error;
								});
								toolSignal.throwIfAborted();
								observations.push({ call, content: resolved.content });
								if (resolved.evidence) {
									evidence.set(
										resolved.evidence.id,
										resolved.evidence.citation,
									);
								}
								if (resolved.gap) gap = true;
								results.push({
									id: call.id,
									ok: true,
									content: resolved.content,
								});
							}
							return results;
						},
					},
					budget.signal,
				);
				if (terminal) outcome = { status: "failed", code: terminal };
				else if (result.status === "failed")
					outcome = { status: "failed", code: result.code };
				else if (result.status === "cancelled")
					outcome = { status: "cancelled" };
				else {
					const validated = validateAnswer(result.text, evidence, gap);
					outcome =
						scope && validated.status === "needs_clarification"
							? { status: "failed", code: "INVALID_ANSWER_STRUCTURE" }
							: validated;
				}
			}
		} catch {
			if (terminal) outcome = { status: "failed", code: terminal };
		}
		if (signal.aborted) outcome = { status: "cancelled" };
		else if (budget.deadline.aborted)
			outcome = { status: "failed", code: "TURN_TIMEOUT" };
		// 不因中止打断提交动作。没有成功提交时，调用者不会收到模型正文。
		let committed: CommittedTurn;
		try {
			const clarification = this.clarification.finish(input, turnId, outcome);
			committed = await session.commit({
				turnId,
				outcome,
				...(clarification !== undefined ? { clarification } : {}),
			});
		} catch (error) {
			this.diagnose({
				...budget.summary(),
				sessionId: session.sessionId,
				turnId,
				status: "failed",
				errorCode: "SESSION_FAILED",
				elapsedMs: Math.round(performance.now() - started),
			});
			throw error;
		}
		this.diagnose({
			...budget.summary(),
			sessionId: session.sessionId,
			turnId,
			status: outcome.status,
			...(outcome.status === "failed" ? { errorCode: outcome.code } : {}),
			elapsedMs: Math.round(performance.now() - started),
		});
		return committed;
	}
}

import type { QueryScope } from "../domain/query-scope.js";
import { Clarification, type DialogueTurn } from "./clarification.js";
