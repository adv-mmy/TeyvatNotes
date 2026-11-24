import type { QueryScope } from "../domain/query-scope.js";
import { Clarification, type DialogueTurn } from "./clarification.js";
import {
	type AgentRunHost,
	type AgentRuntime,
	type JsonObject,
	type JsonValue,
	type ToolBatch,
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

export type FrozenQueryScope = Readonly<QueryScope & { scopeId: string }>;

interface EvidenceView {
	readonly citations: ReadonlyMap<string, CitationSnapshot>;
	readonly gap: boolean;
}
export interface TurnToolEnvironment {
	readonly definitions: readonly ToolDefinition[];
	executeBatch(batch: ToolBatch): Promise<readonly ToolResult[]>;
	confirmDelivery(callIds: readonly string[]): void;
	evidenceView(): EvidenceView;
	close(): void;
}
export type TurnToolEnvironmentFactory = (context: {
	readonly turnId: string;
	readonly scope?: FrozenQueryScope;
	readonly budget: TurnBudget;
	readonly onProgress?: TurnInput["onProgress"];
}) => TurnToolEnvironment;

type ToolFailure = "BUDGET_EXHAUSTED" | "TOOL_TIMEOUT" | "TOOL_FAILED";
export class TurnToolError extends Error {
	constructor(readonly code: ToolFailure) {
		super(code);
	}
}

/** 已注册工具的通用执行环境。正式攻略工具接入后由装配层注入对应工厂。 */
export function registeredTurnTools(
	tools: readonly TurnTool[],
): TurnToolEnvironmentFactory {
	const registered = new Map(tools.map((tool) => [tool.definition.name, tool]));
	const definitions = tools.map((tool) => tool.definition);
	return ({ budget, onProgress }) => {
		type Effect = Awaited<ReturnType<TurnTool["execute"]>>;
		const observations: TurnToolObservation[] = [];
		const citations = new Map<string, CitationSnapshot>();
		let gap = false;
		let closed = false;
		let executing = false;
		let pending:
			| {
					ids: ReadonlySet<string>;
					effects: readonly { call: ToolCall; effect: Effect }[];
			  }
			| undefined;
		function assertActive() {
			budget.signal.throwIfAborted();
			if (closed) throw new TurnToolError("TOOL_FAILED");
		}
		return {
			definitions,
			async executeBatch(batch) {
				assertActive();
				batch.signal.throwIfAborted();
				if (pending || executing) throw new TurnToolError("TOOL_FAILED");
				executing = true;
				try {
					// 整批只看之前已回送的状态，分派开始前做完授权判定。
					const previous = observations.slice();
					const authorized = batch.calls.map((call) => {
						const tool = registered.get(call.name);
						return {
							call,
							tool: tool?.authorize(call, previous) ? tool : undefined,
						};
					});
					budget.recordRejection(batch.rejected.length);
					const effects: { call: ToolCall; effect: Effect }[] = [];
					const results: ToolResult[] = [];
					for (const { call, tool } of authorized) {
						assertActive();
						batch.signal.throwIfAborted();
						if (!tool) {
							budget.recordRejection();
							results.push({
								id: call.id,
								ok: false,
								code: "TOOL_NOT_ALLOWED",
							});
							continue;
						}
						const timeout = budget.startTool();
						if (!timeout) throw new TurnToolError("BUDGET_EXHAUSTED");
						const signal = AbortSignal.any([
							batch.signal,
							budget.signal,
							timeout,
						]);
						onProgress?.("tool");
						const effect = await untilAborted(
							() => tool.execute(call, signal),
							signal,
						).catch(() => {
							throw new TurnToolError(
								timeout.aborted ? "TOOL_TIMEOUT" : "TOOL_FAILED",
							);
						});
						signal.throwIfAborted();
						assertActive();
						effects.push({ call, effect });
						results.push({ id: call.id, ok: true, content: effect.content });
					}
					pending = {
						ids: new Set(
							[...batch.calls, ...batch.rejected].map(({ id }) => id),
						),
						effects,
					};
					return results;
				} finally {
					executing = false;
				}
			},
			confirmDelivery(callIds) {
				assertActive();
				if (
					!pending ||
					pending.ids.size !== callIds.length ||
					new Set(callIds).size !== callIds.length ||
					!callIds.every((id) => pending?.ids.has(id))
				)
					throw new TurnToolError("TOOL_FAILED");
				for (const { call, effect } of pending.effects) {
					observations.push({ call, content: effect.content });
					if (effect.evidence)
						citations.set(
							effect.evidence.id,
							structuredClone(effect.evidence.citation),
						);
					if (effect.gap) gap = true;
				}
				pending = undefined;
			},
			evidenceView() {
				return { citations: new Map(citations), gap };
			},
			close() {
				closed = true;
				pending = undefined;
				observations.length = 0;
				citations.clear();
				gap = false;
			},
		};
	};
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

function freezeScope(scope: QueryScope, turnId: string): FrozenQueryScope {
	const freezeItems = (items: QueryScope["playerConstraints"]["teammates"]) =>
		items.kind === "known"
			? Object.freeze({
					kind: "known" as const,
					items: Object.freeze([...items.items]),
				})
			: Object.freeze({ kind: "unknown" as const });
	return Object.freeze({
		...scope,
		scopeId: turnId,
		playerConstraints: Object.freeze({
			targetHolding: scope.playerConstraints.targetHolding,
			teammates: freezeItems(scope.playerConstraints.teammates),
			equipment: freezeItems(scope.playerConstraints.equipment),
		}),
	});
}

const runtimeFailure = (): TurnOutcome => ({
	status: "failed",
	code: "RUNTIME_FAILED",
});

export class RunTurn {
	constructor(
		private readonly runtime: AgentRuntime,
		private readonly limits: TurnLimits,
		private readonly createToolEnvironment: TurnToolEnvironmentFactory,
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
		const started = performance.now();
		try {
			const turnId = await session.begin(input.prompt);
			let recorded: TurnOutcome = { status: "failed", code: "SESSION_FAILED" };
			try {
				// 确认写入失败属于会话故障；不调用模型，也不尝试补写正常终态。
				if (input.confirmation && !budget.signal.aborted)
					await session.appendConfirmedConditions({
						turnId,
						...input.confirmation,
					});
				const proposed = budget.signal.aborted
					? runtimeFailure()
					: (input.preflight ??
						(await this.runAgent(session, input, turnId, budget)));
				const outcome: TurnOutcome = signal.aborted
					? { status: "cancelled" }
					: budget.deadline.aborted
						? { status: "failed", code: "TURN_TIMEOUT" }
						: proposed;
				const clarification = this.clarification.finish(input, turnId, outcome);
				// 运行取消不能打断持久化。
				const committed = await session.commit({
					turnId,
					outcome,
					...(clarification !== undefined ? { clarification } : {}),
				});
				recorded = committed.outcome;
				return committed;
			} finally {
				this.diagnose({
					...budget.summary(),
					sessionId: session.sessionId,
					turnId,
					status: recorded.status,
					...(recorded.status === "failed" ? { errorCode: recorded.code } : {}),
					elapsedMs: Math.round(performance.now() - started),
				});
			}
		} finally {
			budget.close();
		}
	}

	private async runAgent(
		session: WritableSession,
		input: TurnInput,
		turnId: string,
		budget: TurnBudget,
	): Promise<TurnOutcome> {
		const scope = input.scope ? freezeScope(input.scope, turnId) : undefined;
		let environment: TurnToolEnvironment | undefined;
		let terminal: ToolFailure | undefined;
		try {
			const tools = this.createToolEnvironment({
				turnId,
				budget,
				...(scope ? { scope } : {}),
				...(input.onProgress ? { onProgress: input.onProgress } : {}),
			});
			environment = tools;
			const host: AgentRunHost = {
				prepareModelRequest: async (draft) => {
					if (terminal === "BUDGET_EXHAUSTED")
						return { allowed: false, code: terminal };
					if (terminal) throw new TurnToolError(terminal);
					const prepared = budget.prepare(draft);
					if (prepared.allowed) input.onProgress?.("model");
					return prepared;
				},
				recordModelUsage: (tokens) => {
					if (!budget.recordUsage(tokens)) terminal ??= "BUDGET_EXHAUSTED";
				},
				executeToolBatch: async (batch) => {
					if (terminal) throw new TurnToolError(terminal);
					try {
						return await tools.executeBatch(batch);
					} catch (error) {
						if (error instanceof TurnToolError) terminal ??= error.code;
						throw error;
					}
				},
				toolResultsDelivered: (callIds) => tools.confirmDelivery(callIds),
			};
			const history = session.history();
			const conditions: JsonObject = scope
				? { queryScope: { ...scope } }
				: history.conditions;
			const result = await this.runtime.run(
				{
					prompt: input.prompt,
					tools: tools.definitions,
					turnId,
					priorTurns: scope ? [] : trustedHistory(history),
					conditions,
				},
				host,
				budget.signal,
			);
			if (terminal) return { status: "failed", code: terminal };
			if (result.status === "failed")
				return { status: "failed", code: result.code };
			if (result.status === "cancelled") return { status: "cancelled" };
			const { citations, gap } = tools.evidenceView();
			const validated = validateAnswer(result.text, citations, gap);
			// 攻略轮次尚未接入领域校验，不能凭未校验的模型文字创建证据驱动追问。
			return scope && validated.status === "needs_clarification"
				? { status: "failed", code: "INVALID_ANSWER_STRUCTURE" }
				: validated;
		} catch {
			return terminal ? { status: "failed", code: terminal } : runtimeFailure();
		} finally {
			environment?.close();
		}
	}
}
