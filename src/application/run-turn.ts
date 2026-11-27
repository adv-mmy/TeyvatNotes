import {
	type GuideStructureDetail,
	validateGuideAnswer,
} from "../domain/answer-validation.js";
import type {
	FrozenQueryScope,
	QueryScope,
	TerminologyEntry,
} from "../domain/query-scope.js";
import { Clarification, type DialogueTurn } from "./clarification.js";
import {
	type ToolFailure,
	type TurnToolEnvironment,
	type TurnToolEnvironmentFactory,
	TurnToolError,
} from "./guide-tools.js";
import type { AgentRunHost, AgentRuntime, JsonObject } from "./ports/agent.js";
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

type TurnProposal = TurnOutcome & {
	readonly equipmentId?: string;
	readonly validationDetail?: GuideStructureDetail;
};

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
			...(scope.playerConstraints.noTeammates ? { noTeammates: true } : {}),
			...(scope.playerConstraints.limitEquipment
				? { limitEquipment: true }
				: {}),
			...(scope.playerConstraints.excludedCharacters
				? {
						excludedCharacters: Object.freeze([
							...scope.playerConstraints.excludedCharacters,
						]),
					}
				: {}),
			...(scope.playerConstraints.excludedEquipment
				? {
						excludedEquipment: Object.freeze([
							...scope.playerConstraints.excludedEquipment,
						]),
					}
				: {}),
			...(scope.playerConstraints.ownedEquipment
				? {
						ownedEquipment: Object.freeze([
							...scope.playerConstraints.ownedEquipment,
						]),
					}
				: {}),
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
				validationDetail?: GuideStructureDetail;
				elapsedMs: number;
			},
		) => void = () => {},
		private readonly clarification = new Clarification(),
		private readonly terminology: readonly TerminologyEntry[] = [],
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
			let validationDetail: GuideStructureDetail | undefined;
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
				const outcome: TurnProposal = signal.aborted
					? { status: "cancelled" }
					: budget.deadline.aborted
						? { status: "failed", code: "TURN_TIMEOUT" }
						: proposed;
				const equipmentId =
					outcome.status === "needs_clarification"
						? outcome.equipmentId
						: undefined;
				const clarification = this.clarification.finish(
					input,
					turnId,
					outcome,
					equipmentId,
				);
				const persisted: TurnOutcome =
					outcome.status === "needs_clarification"
						? { status: "needs_clarification", text: outcome.text }
						: outcome.status === "failed"
							? { status: "failed", code: outcome.code }
							: outcome;
				// 运行取消不能打断持久化。
				const committed = await session.commit({
					turnId,
					outcome: persisted,
					...(clarification !== undefined ? { clarification } : {}),
				});
				recorded = committed.outcome;
				if (
					recorded.status === "failed" &&
					recorded.code === "INVALID_ANSWER_STRUCTURE"
				)
					validationDetail = outcome.validationDetail;
				return committed;
			} finally {
				this.diagnose({
					...budget.summary(),
					sessionId: session.sessionId,
					turnId,
					status: recorded.status,
					...(recorded.status === "failed" ? { errorCode: recorded.code } : {}),
					...(validationDetail ? { validationDetail } : {}),
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
	): Promise<TurnProposal> {
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
					...(tools.answerInstructions
						? { answerInstructions: tools.answerInstructions }
						: {}),
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
			const { citations, gap, guide } = tools.evidenceView();
			if (scope && guide)
				return validateGuideAnswer(
					result.text,
					scope,
					turnId,
					guide,
					this.terminology,
				);
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
