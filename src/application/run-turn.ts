import {
	type AgentRuntime,
	type JsonObject,
	type JsonValue,
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

const tools: readonly ToolDefinition[] = Object.entries({
	probe: "在受控资料集检索 sample 或 missing",
	read_candidate: "回读上一批探查返回的候选",
	report_gap: "确认上一批探查返回的缺口",
}).map(([name, description]) => ({
	name,
	description,
	parameters: {
		type: "object",
		properties: { value: { type: "string", minLength: 1, maxLength: 64 } },
		required: ["value"],
		additionalProperties: false,
	},
}));

const sample: CitationSnapshot = {
	source: "TeyvatNotes 隔离测试样本",
	version: "V1",
	location: "fixture:sample/1",
	excerpt: "这是仅用于验证回读和引用的模拟资料。",
};

type Action = "probe" | "read_candidate" | "report_gap";

export interface TurnInput {
	readonly prompt: string;
	readonly confirmedConditions?: JsonObject;
	readonly onProgress?: (stage: "model" | "tool") => void;
}
type ResolvedTool = {
	readonly content: JsonValue;
	readonly decision?: "sample" | "missing";
	readonly citation?: CitationSnapshot;
	readonly gap?: true;
};

export type ControlledToolExecutor = (
	action: Action,
	value: string,
	signal: AbortSignal,
) => Promise<ResolvedTool>;

export const executeControlledTool: ControlledToolExecutor = async (
	action,
	value,
	signal,
) => {
	signal.throwIfAborted();
	if (action === "probe") {
		const decision = value === "sample" ? "sample" : "missing";
		return { content: { decision }, decision };
	}
	if (action === "read_candidate")
		return { content: { evidenceId: "sample_1", ...sample }, citation: sample };
	return { content: { gap: true }, gap: true };
};

function validateAnswer(
	text: string,
	citation: CitationSnapshot | undefined,
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
			value.evidenceIds.length !== 1 ||
			value.evidenceIds[0] !== "sample_1" ||
			!citation
		)
			return { status: "failed", code: "INVALID_CITATION" };
		return { status: "answered", text: value.text, citations: [citation] };
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

export class RunTurn {
	constructor(
		private readonly runtime: AgentRuntime,
		private readonly limits: TurnLimits,
		private readonly tool: ControlledToolExecutor = executeControlledTool,
		private readonly diagnose: (
			info: ReturnType<TurnBudget["summary"]> & {
				sessionId: string;
				turnId: string;
				status: TurnOutcome["status"];
				errorCode?: string;
				elapsedMs: number;
			},
		) => void = () => {},
	) {}

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
		{ prompt, confirmedConditions, onProgress }: TurnInput,
		signal: AbortSignal,
		budget: TurnBudget,
	): Promise<CommittedTurn> {
		const started = performance.now();
		const turnId = await session.begin(prompt);
		let outcome: TurnOutcome = { status: "failed", code: "RUNTIME_FAILED" };
		let decision: "sample" | "missing" | undefined;
		let citation: CitationSnapshot | undefined;
		let gap = false;
		let terminal:
			| "BUDGET_EXHAUSTED"
			| "TOOL_TIMEOUT"
			| "TOOL_FAILED"
			| undefined;
		try {
			if (confirmedConditions && !budget.signal.aborted)
				await session.appendConfirmedConditions({
					turnId,
					conditions: confirmedConditions,
				});
			const history = session.history();
			const result = await this.runtime.run(
				{
					prompt,
					tools,
					turnId,
					priorTurns: trustedHistory(history),
					conditions: history.conditions,
				},
				{
					prepareModelRequest: async (draft) => {
						const prepared = budget.prepare(draft);
						if (prepared.allowed) onProgress?.("model");
						return prepared;
					},
					recordModelUsage: (tokens) => budget.recordUsage(tokens),
					executeToolBatch: async (batch) => {
						// 本批的 probe 结果不得给同一批的 read 或 report 授权。
						const authorized = decision;
						const results: ToolResult[] = [];
						budget.recordRejection(batch.rejected.length);
						for (const call of batch.calls) {
							batch.signal.throwIfAborted();
							const value = call.arguments.value;
							const allowed =
								typeof value === "string" &&
								((call.name === "probe" &&
									(value === "sample" || value === "missing")) ||
									(call.name === "read_candidate" &&
										authorized === "sample" &&
										value === authorized) ||
									(call.name === "report_gap" &&
										authorized === "missing" &&
										value === authorized));
							if (!allowed || typeof value !== "string") {
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
								() => this.tool(call.name as Action, value, toolSignal),
								toolSignal,
							).catch((error: unknown) => {
								terminal = timeout.aborted ? "TOOL_TIMEOUT" : "TOOL_FAILED";
								throw error;
							});
							toolSignal.throwIfAborted();
							if (resolved.decision) decision = resolved.decision;
							if (resolved.citation) citation = resolved.citation;
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
			else if (result.status === "cancelled") outcome = { status: "cancelled" };
			else outcome = validateAnswer(result.text, citation, gap);
		} catch {
			if (terminal) outcome = { status: "failed", code: terminal };
		}
		if (signal.aborted) outcome = { status: "cancelled" };
		else if (budget.deadline.aborted)
			outcome = { status: "failed", code: "TURN_TIMEOUT" };
		// 不因中止打断提交动作。没有成功提交时，调用者不会收到模型正文。
		let committed: CommittedTurn;
		try {
			committed = await session.commit({ turnId, outcome });
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
