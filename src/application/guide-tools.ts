import { randomUUID } from "node:crypto";
import type {
	GuideEvidence,
	GuideEvidenceView,
} from "../domain/answer-validation.js";
import { isSupplementGap } from "../domain/answer-validation.js";
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
import type { WebSupplement } from "./ports/guide-source.js";
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
	authorize(
		call: ToolCall,
		previous: readonly TurnToolObservation[],
		batch: ToolBatch,
	): boolean | ToolRejection;
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
	| "WEB_SOURCE_UNAVAILABLE"
	| "INDEX_NOT_READY"
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
						const decision = tool?.authorize(call, previous, batch);
						return {
							call,
							tool: decision === true ? tool : undefined,
							rejection:
								typeof decision === "string"
									? decision
									: (tool?.denyCode ?? "TOOL_NOT_ALLOWED"),
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

export function createGuideTools(
	retrieval: GuideRetrieval,
	supplement?: WebSupplement,
): TurnToolEnvironmentFactory {
	return (context) => {
		const { scope, turnId } = context;
		if (!scope) throw new TurnToolError("TOOL_FAILED");
		const candidates = new Map<string, AuthorizedCandidate>();
		let webSpent = false;
		let recheck: string | undefined;
		let lastMiss: string | undefined;
		const tools: readonly TurnTool[] = [
			{
				definition: schemaOf(
					"search_web_guides",
					"仅在前一批已确认库内未命中时，按角色、版本和配队目标补查一次。",
					"query",
				),
				authorize(call, previous, batch) {
					if (
						webSpent ||
						candidates.size ||
						[...batch.calls, ...batch.rejected].some(
							(c) => c.name === "search_guides",
						) ||
						!previous.some(
							(o) =>
								o.call.name === "search_guides" &&
								isObject(o.content) &&
								o.content.status === "miss",
						)
					)
						return false;
					const q = call.arguments.query;
					if (typeof q !== "string") return "INVALID_TOOL_ARGUMENT";
					let residue = q.normalize("NFKC").trim();
					if (!residue || /(?:\b(?:OR|AND|NOT)\b|[:："'()-])/iu.test(residue))
						return "INVALID_TOOL_ARGUMENT";
					for (const term of [
						scope.entityId,
						scope.gameVersion,
						"原神",
						"配队",
						"阵容",
						"攻略",
						"推荐",
					])
						residue = residue.replaceAll(term, "");
					if (residue.trim()) return "INVALID_TOOL_ARGUMENT";
					// 同批多个合法补查也只给第一个执行资格。
					webSpent = true;
					return true;
				},
				async execute(_call, signal) {
					if (!context.budget.canFinishSupplement)
						throw new TurnToolError("BUDGET_EXHAUSTED");
					const status = supplement
						? await supplement(scope, context.budget, signal)
						: "source_restricted";
					if (status === "indexed") {
						// 重检是必经步骤，预算已尽时也不再花一次模型请求来提出重检。
						if (!context.budget.hasToolCapacity)
							throw new TurnToolError("BUDGET_EXHAUSTED");
						recheck = lastMiss;
					}
					return { content: { status } };
				},
			},
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
						recheck ?? query.trim(),
						signal,
					);
					signal.throwIfAborted();
					if (result.kind === "error") throw new TurnToolError(result.code);
					const afterSupplement = recheck !== undefined;
					const searched = recheck ?? query.trim();
					recheck = undefined;
					if (result.kind === "miss") {
						lastMiss = searched;
						return {
							content: {
								status: "miss",
								...(afterSupplement ? { reason: "recheck_miss" } : {}),
							},
							gap: true,
						};
					}
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
			const web = observations.find(
				(o) => o.call.name === "search_web_guides",
			)?.content;
			return {
				evidence,
				gap: gap && recheck === undefined,
				readAllCandidates,
				...(!candidates.size &&
				observations.some(
					(o) =>
						o.call.name === "search_guides" &&
						isObject(o.content) &&
						o.content.reason === "recheck_miss",
				)
					? { supplementGap: "recheck_miss" as const }
					: !candidates.size && isObject(web) && isSupplementGap(web.status)
						? { supplementGap: web.status }
						: {}),
			};
		})(context);
		return {
			...env,
			answerInstructions: [
				"工具阶段直接发出工具调用，不写答复。结束时只输出一个裸 JSON 对象：首字符是 {，末字符是 }。不要加 Markdown 代码围栏、开场白、结语或额外字段。",
				"顶层必须同时有 recommendations 和 gaps，两个字段都必须是数组。能给建议时 recommendations 至少一项、gaps 必须是 []；资料不足、确需确认装备或证据冲突时 recommendations 必须是 []、gaps 必须恰好一项。不要省略空数组，不要同时填两个数组。",
				"先检索再在下一批完整回读。摘要不是证据。所有 evidenceIds 都是非空字符串数组，使用 read_guide 返回的真实 evidenceId，不填候选 ID、链接或示例占位符。",
				"只有查库明确 miss 后才可在不含查库的下一批用 search_web_guides 补查一次。query 仅用本轮角色、版本、配队。indexed 后必须用原范围重新查库、回读；其他状态说明具体缺口。",
				"每项建议必须包含 text、conditions、requiredEntityIds、evidenceIds。conditions 保留回读给出的明确适用前提；requiredEntityIds 仅为所引证据的 requiredCharacterIds 和 requiredEquipmentIds 的去重并集。没有前提或必需实体时填 []。按库存配队时，只把已确认可用的队伍推荐为可用，不虚构原文未给出的结论。",
				"以下是四种输出形状。只选一种，例文与占位符不能照抄进实际答复。",
				'有建议：{"recommendations":[{"text":"有据可查的建议","conditions":[],"requiredEntityIds":[],"evidenceIds":["<本轮证据ID>"]}],"gaps":[]}',
				'资料不足：{"recommendations":[],"gaps":[{"kind":"insufficient_evidence","text":"无法取得适用的完整证据"}]}',
				'完整回读发现有必需但持有未知的装备且无可用替代：{"recommendations":[],"gaps":[{"kind":"equipment_required","equipmentId":"<必需装备标准名>","evidenceIds":["<本轮证据ID>"]}]}',
				'回读后仍有冲突：{"recommendations":[],"gaps":[{"kind":"conflict","sides":[{"conclusion":"观点一","conditions":[],"evidenceIds":["<本轮甲方证据ID>"]},{"conclusion":"观点二","conditions":[],"evidenceIds":["<本轮乙方证据ID>"]}]}]}',
			].join("\n"),
			close() {
				env.close();
				candidates.clear();
			},
		};
	};
}
