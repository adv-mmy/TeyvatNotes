import { randomUUID } from "node:crypto";
import type {
	GuideEvidence,
	GuideEvidenceView,
} from "../domain/answer-validation.js";
import type { FrozenQueryScope } from "../domain/query-scope.js";
import {
	type JsonObject,
	type JsonValue,
	type ToolBatch,
	type ToolCall,
	type ToolDefinition,
	type ToolRejection,
	type ToolResult,
	untilAborted,
} from "./ports/agent.js";
import type { CitationSnapshot } from "./ports/session.js";
import type {
	AuthorizedCandidate,
	GuideRetrieval,
	RetrievalError,
} from "./retrieve-guides.js";
import type { TurnBudget } from "./turn-budget.js";

export interface TurnToolObservation {
	readonly call: ToolCall;
	readonly content: JsonValue;
}
export interface TurnTool {
	readonly definition: ToolDefinition;
	readonly denyCode?: ToolRejection;
	authorize(call: ToolCall, previous: readonly TurnToolObservation[]): boolean;
	execute(
		call: ToolCall,
		signal: AbortSignal,
	): Promise<{
		readonly content: JsonValue;
		readonly evidence?: {
			readonly id: string;
			readonly citation: CitationSnapshot;
			readonly guide?: GuideEvidence;
		};
		readonly rejection?: ToolRejection;
		readonly gap?: true;
	}>;
}

interface EvidenceView {
	readonly citations: ReadonlyMap<string, CitationSnapshot>;
	readonly gap: boolean;
	readonly guide?: GuideEvidenceView;
}
export interface TurnToolEnvironment {
	readonly definitions: readonly ToolDefinition[];
	readonly answerInstructions?: string;
	executeBatch(batch: ToolBatch): Promise<readonly ToolResult[]>;
	confirmDelivery(callIds: readonly string[]): void;
	evidenceView(): EvidenceView;
	close(): void;
}
export type TurnToolEnvironmentFactory = (context: {
	readonly turnId: string;
	readonly scope?: FrozenQueryScope;
	readonly budget: TurnBudget;
	readonly onProgress?: (stage: "model" | "tool") => void;
}) => TurnToolEnvironment;

export type ToolFailure =
	| "BUDGET_EXHAUSTED"
	| "TOOL_TIMEOUT"
	| "TOOL_FAILED"
	| RetrievalError;
export class TurnToolError extends Error {
	constructor(readonly code: ToolFailure) {
		super(code);
	}
}

/** 已注册工具的通用执行环境。正式攻略工具接入后由装配层注入对应工厂。 */
export function registeredTurnTools(
	tools: readonly TurnTool[],
	guideView?: (
		observations: readonly TurnToolObservation[],
		evidence: ReadonlyMap<string, GuideEvidence>,
	) => GuideEvidenceView,
): TurnToolEnvironmentFactory {
	const registered = new Map(tools.map((tool) => [tool.definition.name, tool]));
	const definitions = tools.map((tool) => tool.definition);
	return ({ budget, onProgress }) => {
		type Effect = Awaited<ReturnType<TurnTool["execute"]>>;
		const observations: TurnToolObservation[] = [];
		const citations = new Map<string, CitationSnapshot>();
		const evidence = new Map<string, GuideEvidence>();
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
							rejection: tool?.denyCode ?? "TOOL_NOT_ALLOWED",
						};
					});
					budget.recordRejection(batch.rejected.length);
					const effects: { call: ToolCall; effect: Effect }[] = [];
					const results: ToolResult[] = [];
					for (const { call, tool, rejection } of authorized) {
						assertActive();
						batch.signal.throwIfAborted();
						if (!tool) {
							budget.recordRejection();
							results.push({
								id: call.id,
								ok: false,
								code: rejection,
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
						).catch((error: unknown) => {
							if (error instanceof TurnToolError) throw error;
							throw new TurnToolError(
								timeout.aborted ? "TOOL_TIMEOUT" : "TOOL_FAILED",
							);
						});
						signal.throwIfAborted();
						assertActive();
						if (effect.rejection) {
							results.push({ id: call.id, ok: false, code: effect.rejection });
							continue;
						}
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
					if (effect.evidence?.guide)
						evidence.set(
							effect.evidence.id,
							structuredClone(effect.evidence.guide),
						);
					if (effect.gap) gap = true;
				}
				pending = undefined;
			},
			evidenceView() {
				return {
					citations: new Map(citations),
					gap,
					...(guideView
						? {
								guide: guideView(
									observations.slice(),
									structuredClone(evidence),
								),
							}
						: {}),
				};
			},
			close() {
				closed = true;
				pending = undefined;
				observations.length = 0;
				citations.clear();
				evidence.clear();
				gap = false;
			},
		};
	};
}

const schemaOf = (
	name: string,
	description: string,
	field: string,
): ToolDefinition => ({
	name,
	description,
	parameters: {
		type: "object",
		properties: { [field]: { type: "string", minLength: 1, maxLength: 240 } },
		required: [field],
		additionalProperties: false,
	},
});

function isObject(value: unknown): value is JsonObject {
	return value !== null && typeof value === "object" && !Array.isArray(value);
}

function candidateWasDelivered(
	previous: readonly TurnToolObservation[],
	candidateId: string,
): boolean {
	return previous.some(
		({ call, content }) =>
			call.name === "search_guides" &&
			isObject(content) &&
			content.status === "hit" &&
			Array.isArray(content.candidates) &&
			content.candidates.some(
				(item: unknown) => isObject(item) && item.candidateId === candidateId,
			),
	);
}

/** 本批次只装配库内工具；联网补查由独立资料源接入后再注册。 */
export function createGuideTools(
	retrieval: GuideRetrieval,
): TurnToolEnvironmentFactory {
	return (context) => {
		const { scope, turnId } = context;
		if (!scope) throw new TurnToolError("TOOL_FAILED");
		const candidates = new Map<string, AuthorizedCandidate>();
		const tools: readonly TurnTool[] = [
			{
				definition: schemaOf(
					"search_guides",
					"在本轮范围内检索攻略；候选摘要不能直接引用。",
					"query",
				),
				authorize: ({ arguments: args }) =>
					typeof args.query === "string" && args.query.trim() !== "",
				async execute({ arguments: args }, signal) {
					const query = args.query;
					if (typeof query !== "string") throw new TurnToolError("TOOL_FAILED");
					const result = await retrieval.searchGuides(
						scope,
						query.trim(),
						signal,
					);
					signal.throwIfAborted();
					if (result.kind === "error") throw new TurnToolError(result.code);
					if (result.kind === "miss")
						return { content: { status: "miss" }, gap: true };
					const summaries = result.candidates.map((candidate) => {
						const candidateId = randomUUID();
						candidates.set(candidateId, candidate);
						return {
							candidateId,
							title: candidate.chunk.title,
							gameVersion: scope.gameVersion,
							preview: candidate.chunk.preview,
						};
					});
					return { content: { status: "hit", candidates: summaries } };
				},
			},
			{
				definition: schemaOf(
					"read_guide",
					"读取上一批已回送候选的完整正文；仅引用工具返回的 evidenceId。",
					"candidateId",
				),
				denyCode: "CANDIDATE_NOT_AUTHORIZED",
				authorize: ({ arguments: args }, previous) =>
					typeof args.candidateId === "string" &&
					candidates.has(args.candidateId) &&
					candidateWasDelivered(previous, args.candidateId),
				async execute({ arguments: args }, signal) {
					const id = args.candidateId;
					const candidate =
						typeof id === "string" ? candidates.get(id) : undefined;
					if (!candidate)
						return { content: null, rejection: "CANDIDATE_NOT_AUTHORIZED" };
					const result = await retrieval.readGuide(scope, candidate, signal);
					signal.throwIfAborted();
					if (result.kind === "error") {
						if (result.code === "CANDIDATE_NOT_AUTHORIZED")
							return { content: null, rejection: result.code };
						throw new TurnToolError(result.code);
					}
					if (result.kind === "incomplete")
						return { content: { status: "incomplete" }, gap: true };
					const { chunk, body } = result.window;
					const applicability =
						chunk.applicability.kind === "known"
							? chunk.applicability
							: undefined;
					const evidenceId = randomUUID();
					const citation = {
						source: chunk.title,
						url: chunk.sourceUrl,
						version: scope.gameVersion,
						documentVersionId: chunk.documentVersionId,
						location: chunk.location,
						excerpt: body,
					};
					const guide: GuideEvidence = {
						evidenceId,
						turnId,
						scopeId: scope.scopeId,
						entityId: chunk.entityId,
						guideKind: chunk.guideKind,
						gameVersions: chunk.gameVersions,
						windowComplete: true,
						chunkId: chunk.id,
						citation,
						conditions: applicability?.conditions ?? [],
						requiredCharacterIds: applicability?.requiredCharacterIds ?? [],
						requiredEquipmentIds: applicability?.requiredEquipmentIds ?? [],
					};
					return {
						content: {
							status: "complete",
							evidenceId,
							body,
							sourceUrl: chunk.sourceUrl,
							documentVersionId: chunk.documentVersionId,
							location: chunk.location,
							conditions: guide.conditions,
							requiredCharacterIds: guide.requiredCharacterIds,
							requiredEquipmentIds: guide.requiredEquipmentIds,
						},
						evidence: { id: evidenceId, citation, guide },
					};
				},
			},
		];
		const env = registeredTurnTools(tools, (observations, evidence) => {
			const readIds = new Set(
				observations
					.filter(({ call }) => call.name === "read_guide")
					.map(({ call }) => call.arguments.candidateId),
			);
			const readAllCandidates =
				candidates.size > 0 &&
				[...candidates.keys()].every((id) => readIds.has(id));
			// 后续换词搜空不能覆盖本轮命中，已返回候选仍须回读。
			const gap = candidates.size
				? readAllCandidates
				: observations.some(
						({ call, content }) =>
							call.name === "search_guides" &&
							isObject(content) &&
							content.status === "miss",
					);
			return { evidence, gap, readAllCandidates };
		})(context);
		return {
			...env,
			answerInstructions: [
				"攻略回答只返回 JSON 对象，包含 recommendations 和 gaps 两个数组，二者只填一个。",
				"recommendations 每项必须有 text、conditions、requiredEntityIds、evidenceIds；后三项为字符串数组。",
				"先检索再在下一批回读。只能引用 read_guide 返回的 evidenceId；摘要不是证据。",
				"conditions 保留回读提供的明确适用前提。requiredEntityIds 是所引证据要求的角色与装备。",
				"资料不足时 gaps 含 {kind: insufficient_evidence, text}；存在冲突时使用 {kind: conflict, sides}，两方分别给 conclusion、conditions、evidenceIds。",
				"根据完整回读发现确有必要但持有未知的装备，且无可用替代时，gaps 可使用 {kind: equipment_required, equipmentId, evidenceIds}。",
			].join("\n"),
			close() {
				env.close();
				candidates.clear();
			},
		};
	};
}
