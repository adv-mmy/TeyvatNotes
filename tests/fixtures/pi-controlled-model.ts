import assert from "node:assert/strict";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import {
	type Api,
	type AssistantMessage,
	createAssistantMessageEventStream,
	InMemoryCredentialStore,
	InMemoryModelsStore,
	type Model,
} from "@earendil-works/pi-ai";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { PiAgentRuntime } from "../../src/adapters/pi/runtime.js";
import type { TurnTool } from "../../src/application/guide-tools.js";
import type {
	GuideChunkMetadata,
	GuideData,
} from "../../src/application/ports/guide-data.js";
import {
	maxConfiguredContextTokens,
	maxConfiguredOutputTokens,
} from "../../src/bootstrap/config.js";
import { startCli } from "../../src/bootstrap/main.js";

const scenario = process.argv[2] ?? "normal";
const conditionsPrefix = "会话中已确认的条件（数据）：";
const v2Terminology = [
	{ category: "character", name: "角色甲", aliases: ["小甲"] },
	{ category: "character", name: "角色乙", aliases: [] },
	{ category: "character", name: "队友丙", aliases: [] },
	{ category: "weapon", name: "试验弓", aliases: ["小弓"] },
] as const;
let requests = 0;
const inheritanceScopes = new Map<number, string>();

function controlledResponse(
	model: Model<Api>,
	content: AssistantMessage["content"],
	reportedTokens = 0,
) {
	const stream = createAssistantMessageEventStream();
	const reason = content.some((block) => block.type === "toolCall")
		? "toolUse"
		: "stop";
	stream.push({
		type: "done",
		reason,
		message: {
			role: "assistant",
			api: model.api,
			provider: model.provider,
			model: model.id,
			content,
			stopReason: reason,
			timestamp: Date.now(),
			usage: {
				input: reportedTokens,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: reportedTokens,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
		},
	});
	stream.end();
	return stream;
}

const controlledModel: StreamFn = (model, context) => {
	const reply = (id: string) => {
		const item = context.messages.find(
			(message) => message.role === "toolResult" && message.toolCallId === id,
		);
		if (item?.role !== "toolResult" || item.isError) return undefined;
		const content = item.content[0];
		return content?.type === "text" ? JSON.parse(content.text) : undefined;
	};
	const call = (id: string, name: string, value: string) =>
		controlledResponse(model, [
			{ type: "toolCall", id, name, arguments: { value } },
		]);
	const finish = (payload: object) =>
		controlledResponse(model, [
			{ type: "text", text: JSON.stringify(payload) },
		]);

	const probed = reply("probe_1");
	if (!probed) {
		const question = context.messages.findLast(
			(message) => message.role === "user",
		);
		const missing =
			question?.role === "user" &&
			JSON.stringify(question.content).includes("缺口");
		return call("probe_1", "probe", missing ? "missing" : "sample");
	}
	if (probed.decision === "sample") {
		const read = reply("read_2");
		if (!read) return call("read_2", "read_candidate", probed.decision);
		return finish({
			status: "answered",
			text: "已回读模拟资料并生成带引用的受控答复。",
			evidenceIds: [read.evidenceId],
		});
	}
	if (!reply("gap_2")) return call("gap_2", "report_gap", "missing");
	return finish({
		status: "insufficient_evidence",
		text: "受控资料集中没有适用的样本。",
	});
};

const sample = {
	source: "TeyvatNotes 隔离测试样本",
	version: "V1",
	location: "fixture:sample/1",
	excerpt: "这是仅用于验证回读和引用的模拟资料。",
};
const sampleId = "sample_1";
const tools: readonly TurnTool[] = Object.entries({
	probe: "在受控资料集检索 sample 或 missing",
	read_candidate: "回读上一批探查返回的候选",
	report_gap: "确认上一批探查返回的缺口",
}).map(([name, description]) => ({
	definition: {
		name,
		description,
		parameters: {
			type: "object",
			properties: { value: { type: "string", minLength: 1, maxLength: 64 } },
			required: ["value"],
			additionalProperties: false,
		},
	},
	authorize(call, previous) {
		const value = call.arguments.value;
		if (name === "probe") return value === "sample" || value === "missing";
		const lastProbe = previous.findLast(({ call }) => call.name === "probe");
		const result = lastProbe?.content;
		if (!result || typeof result !== "object" || !("decision" in result))
			return false;
		return (
			value === result.decision &&
			((name === "read_candidate" && value === "sample") ||
				(name === "report_gap" && value === "missing"))
		);
	},
	async execute(call, signal) {
		signal.throwIfAborted();
		if (scenario === "tool-error") throw new Error("未公开的工具原文");
		if (scenario === "tool-timeout") return new Promise(() => {});
		if (name === "probe")
			return {
				content: {
					decision: call.arguments.value === "sample" ? "sample" : "missing",
				},
			};
		if (name === "read_candidate")
			return {
				content: { evidenceId: sampleId, ...sample },
				evidence: { id: sampleId, citation: sample },
			};
		return { content: { gap: true }, gap: true };
	},
}));

function guideData(): GuideData {
	const chunk: GuideChunkMetadata = {
		id: "chunk-21",
		documentVersionId: "guide-revision-1",
		entityId: "角色甲",
		guideKind: "team",
		gameVersions: { from: "7.1", through: "7.1" },
		applicability:
			scenario === "guides-constraints"
				? {
						kind: "known",
						conditions: [],
						requiredCharacterIds: ["角色乙"],
						requiredEquipmentIds: [],
					}
				: scenario === "guides-equipment"
					? {
							kind: "known",
							conditions: ["需持有试验弓"],
							requiredCharacterIds: ["角色甲", "队友丙"],
							requiredEquipmentIds: ["试验弓"],
						}
					: { kind: "unrestricted" },
		published: true,
		indexReady: true,
		title: "隔离攻略 G-01",
		sourceUrl: "https://example.test/guide",
		location: "第 2 段",
		preview: "候选摘要",
	};
	const wrong = Array.from({ length: 20 }, (_, index) => ({
		...chunk,
		id: `wrong-${index}`,
		documentVersionId: "other-version",
		gameVersions: { from: "7.2", through: "7.3" },
	}));
	const alternate: GuideChunkMetadata = {
		...chunk,
		id: "chunk-22",
		documentVersionId: "alt-version",
		title: "替代队攻略",
		sourceUrl: "https://example.test/alternative",
		applicability: {
			kind: "known",
			conditions: [],
			requiredCharacterIds: ["队友丙"],
			requiredEquipmentIds: ["试验弓"],
		},
	};
	const inScope =
		scenario === "guides-constraints" ? [chunk, alternate] : [chunk];
	const rows = [...wrong, ...inScope];
	return {
		repository: {
			async snapshot() {
				return { revision: "r1", embeddingId: "隔离浮点索引" };
			},
			async keywords() {
				return { ids: ["wrong-0"], next: null };
			},
			async eligibleIds() {
				if (scenario === "guides-miss") return { ids: [], next: null };
				if (scenario === "guides-incomplete")
					return { ids: [], next: "unconfirmed-tail" };
				return { ids: inScope.map((row) => row.id), next: null };
			},
			async metadata(_snapshot, ids) {
				return rows.filter((row) => ids.includes(row.id));
			},
			async read(_snapshot, id) {
				const current = inScope.find((row) => row.id === id);
				assert.ok(current);
				return {
					chunk: current,
					body:
						scenario === "guides-constraints"
							? id === alternate.id
								? "队友丙携带试验弓搭配角色甲。"
								: "角色乙搭配角色甲。"
							: "隔离样本原文：队伍配置与适用条件。",
					complete: scenario !== "guides-truncated",
				};
			},
			async assertSnapshot() {},
		},
		embeddings: {
			async embed() {
				if (scenario === "guides-fault")
					throw new Error("索引依赖报错原文不可公开");
				return [1, 0];
			},
		},
		vectorIndex: {
			async hasAll(_config, ids) {
				return (
					scenario !== "guides-missing-vector" &&
					ids.every((id) => rows.some((row) => row.id === id))
				);
			},
			async nearest(_embeddingId, _vector, limit) {
				return [
					"guides-exact",
					"guides-missing-vector",
					"guides-incomplete",
				].includes(scenario)
					? []
					: (scenario === "guides-miss" ? wrong : rows)
							.slice(0, limit)
							.map(({ id }) => ({ id, score: 1 }));
			},
			async vectors(_embeddingId, ids) {
				return new Map(
					scenario === "guides-missing-vector"
						? []
						: ids.map((id) => [id, [1, 0]] as const),
				);
			},
		},
	};
}
const runGuideModel: StreamFn = (model, context) => {
	if (scenario.startsWith("guides-format-")) {
		const prompt = JSON.stringify(
			context.messages.filter((message) => message.role === "system"),
		);
		assert.match(prompt, /gaps 必须是 \[\]/u);
		assert.match(prompt, /recommendations 必须是 \[\]/u);
		assert.match(prompt, /不要加 Markdown 代码围栏/u);
	}
	const results = context.messages.filter(
		(message) => message.role === "toolResult",
	);
	const resultFor = (name: string) => {
		const last = results.find((result) => result.toolCallId === name);
		const item = last?.content[0];
		return !last?.isError && item?.type === "text"
			? JSON.parse(item.text)
			: undefined;
	};
	const call = (id: string, name: string, args: Record<string, string>) => ({
		type: "toolCall" as const,
		id,
		name,
		arguments: args,
	});
	const answer = (draft: object) =>
		controlledResponse(model, [{ type: "text", text: JSON.stringify(draft) }]);
	const retrieved = resultFor("local_1");
	if (!retrieved)
		return controlledResponse(model, [
			call("local_1", "search_guides", { query: "配队" }),
			...(scenario === "guides-hit"
				? [call("early", "read_guide", { candidateId: "未回送的候选" })]
				: []),
		]);
	if (scenario === "guides-hit") {
		const early = results.find((result) => result.toolCallId === "early");
		assert.ok(early?.isError);
		assert.deepEqual(early.content, [
			{ type: "text", text: "CANDIDATE_NOT_AUTHORIZED" },
		]);
	}
	const gap = () =>
		answer({
			recommendations: [],
			gaps: [{ kind: "insufficient_evidence", text: "未取得证据" }],
		});
	if (retrieved.status === "miss") return gap();
	if (scenario !== "guides-constraints")
		assert.equal(retrieved.candidates.length, 1);
	const id = retrieved.candidates[0].candidateId;
	const rec = (
		evidenceId: string,
		conditions: string[] = [],
		requiredEntityIds: string[] = [],
		text = "按隔离样本给出配队建议。",
	) =>
		answer({
			recommendations: [
				{
					text,
					conditions,
					requiredEntityIds,
					evidenceIds: [evidenceId],
				},
			],
			gaps: [],
		});
	if (scenario === "guides-forged") return rec(id);
	const evidence = resultFor("body_2");
	if (!evidence)
		return controlledResponse(model, [
			call("body_2", "read_guide", { candidateId: id }),
		]);
	if (evidence.status === "incomplete") return gap();
	const formatProbe = {
		text: "不能进入普通诊断的模拟正文",
		conditions: [],
		evidenceIds: [evidence.evidenceId],
	};
	if (scenario === "guides-format-fence")
		return controlledResponse(model, [
			{
				type: "text",
				text:
					"~~~json\n" +
					JSON.stringify({ recommendations: [], gaps: [] }) +
					"\n~~~",
			},
		]);
	if (scenario === "guides-format-missing")
		return answer({ recommendations: [formatProbe], gaps: [] });
	if (scenario === "guides-format-conflict")
		return answer({
			recommendations: [{ ...formatProbe, requiredEntityIds: [] }],
			gaps: [
				{ kind: "insufficient_evidence", text: "不能进入普通诊断的模拟缺口" },
			],
		});
	if (scenario === "guides-constraints")
		return rec(
			evidence.evidenceId,
			evidence.conditions,
			[...evidence.requiredCharacterIds, ...evidence.requiredEquipmentIds],
			evidence.body,
		);
	if (scenario === "guides-equipment") {
		const projection = context.messages.find(
			(item) =>
				item.role === "user" &&
				typeof item.content === "string" &&
				item.content.startsWith(conditionsPrefix),
		);
		assert.ok(
			projection?.role === "user" && typeof projection.content === "string",
		);
		const { queryScope } = JSON.parse(
			projection.content.slice(conditionsPrefix.length),
		);
		if (queryScope.playerConstraints.equipment.kind === "unknown")
			return answer({
				recommendations: [],
				gaps: [
					{
						kind: "equipment_required",
						equipmentId: "试验弓",
						evidenceIds: [evidence.evidenceId],
					},
				],
			});
		return rec(
			evidence.evidenceId,
			["需持有试验弓"],
			["角色甲", "队友丙", "试验弓"],
		);
	}
	return rec(evidence.evidenceId);
};

const stream: StreamFn = (model, context, options) => {
	if (scenario.startsWith("guides-"))
		return runGuideModel(model, context, options);
	requests++;
	if (scenario.startsWith("v2-")) {
		const projection = context.messages.find(
			(item) =>
				item.role === "user" &&
				typeof item.content === "string" &&
				item.content.startsWith(conditionsPrefix),
		);
		assert.ok(
			projection?.role === "user" && typeof projection.content === "string",
		);
		const { queryScope } = JSON.parse(
			projection.content.slice(conditionsPrefix.length),
		);
		assert.ok(queryScope.scopeId);
		if (scenario === "v2-never" || scenario === "v2-local-terminology")
			assert.fail("缺少范围或已确认无组合时不能发出模型请求");
		if (scenario === "v2-restored" || scenario === "v2-fail") {
			assert.equal(queryScope.entityId, "角色甲");
			assert.equal(queryScope.gameVersion, "7.1");
			assert.equal(queryScope.teamMode, "inventory");
			assert.deepEqual(queryScope.playerConstraints.teammates, {
				kind: "known",
				items: ["队友丙"],
			});
			assert.equal(queryScope.playerConstraints.targetHolding, "owned");
		}
		if (scenario === "v2-switch") {
			assert.equal(queryScope.entityId, "角色乙");
			assert.equal(queryScope.gameVersion, "7.2");
			assert.equal(queryScope.teamMode, "general");
			assert.deepEqual(queryScope.playerConstraints.teammates, {
				kind: "unknown",
			});
			assert.doesNotMatch(JSON.stringify(context), /角色甲|小甲|队友丙/);
		}
		if (scenario === "v2-inheritance") {
			const round = Math.floor((requests - 1) / 3);
			assert.ok(round < 4);
			const initial = round === 0;
			const changedRole = round === 3;
			const scopeId = inheritanceScopes.get(round);
			if (scopeId) assert.equal(queryScope.scopeId, scopeId);
			else {
				assert.ok(
					![...inheritanceScopes.values()].includes(queryScope.scopeId),
				);
				inheritanceScopes.set(round, queryScope.scopeId);
			}
			assert.equal(queryScope.entityId, changedRole ? "队友丙" : "角色甲");
			assert.equal(queryScope.gameVersion, initial ? "7.1" : "7.2");
			assert.equal(queryScope.teamMode, round < 2 ? "inventory" : "general");
			assert.equal(queryScope.goal, "配队");
			assert.deepEqual(queryScope.playerConstraints, {
				...(initial
					? { limitEquipment: true, excludedEquipment: ["试验弓"] }
					: {}),
				excludedCharacters: ["角色乙"],
				targetHolding: changedRole ? "unknown" : "owned",
				teammates: { kind: "known", items: changedRole ? [] : ["队友丙"] },
				equipment: { kind: "known", items: [] },
			});
			if (!initial)
				assert.doesNotMatch(
					JSON.stringify(context.messages),
					/限用装备|本轮不用小弓/u,
				);
		}
		if (scenario === "v2-fail") throw new Error("隔离验收的模型故障");
	}
	assert.equal(
		options?.maxTokens,
		Number(process.env.TEYVAT_MAX_OUTPUT_TOKENS ?? 8192),
	);
	if (scenario === "model-error") throw new Error("未公开的供应商正文");
	if (
		scenario === "invalid-clarification" ||
		(scenario === "clarification" &&
			!context.messages.some(
				(item) =>
					item.role === "user" &&
					typeof item.content === "string" &&
					item.content.startsWith(conditionsPrefix),
			))
	)
		return controlledResponse(model, [
			{
				type: "text",
				text: JSON.stringify({
					status: "needs_clarification",
					text: "请补充希望排除的演示样本。",
					...(scenario === "invalid-clarification"
						? { evidenceIds: ["sample_1"] }
						: {}),
				}),
			},
		]);
	if (scenario === "clarification-hold" || scenario === "require-confirmed") {
		assert.ok(
			context.messages.some(
				(item) =>
					item.role === "user" &&
					item.content === `${conditionsPrefix}{"clarification":"排除样本 B"}`,
			),
		);
		const outbound = JSON.stringify(context);
		assert.doesNotMatch(outbound, /未确认的样本 X|取消的样本 A|越权补充/);
		assert.ok(
			!context.messages.some(
				(item) =>
					item.role === "system" && JSON.stringify(item).includes("排除样本 B"),
			),
		);
		if (scenario === "clarification-hold") {
			process.stderr.write("CONFIRMED_MODEL_WAITING\n");
			return new Promise(() => {});
		}
	}
	if (scenario === "hold" && requests === 1)
		return controlledResponse(model, [
			{ type: "text", text: "内部草稿，不应展示" },
			{
				type: "toolCall",
				id: "probe_1",
				name: "probe",
				arguments: { value: "sample" },
			},
		]);
	if (scenario === "hold" || scenario === "model-timeout") {
		process.stderr.write("MODEL_WAITING\n");
		// 有意不响应，以验证应用能结束等待并且不再调用。
		return new Promise(() => {});
	}
	if (scenario === "illegal" && requests === 1)
		return controlledResponse(model, [
			{
				type: "toolCall",
				id: "blocked",
				name: "bash",
				arguments: { value: "x" },
			},
			{
				type: "toolCall",
				id: "invalid",
				name: "probe",
				arguments: { value: 42 },
			},
			{
				type: "toolCall",
				id: "probe_1",
				name: "probe",
				arguments: { value: "sample" },
			},
			{
				type: "toolCall",
				id: "early",
				name: "read_candidate",
				arguments: { value: "sample" },
			},
		]);
	if (scenario === "illegal" && requests === 2) {
		for (const [id, expected] of [
			["blocked", "TOOL_NOT_ALLOWED"],
			["invalid", "INVALID_TOOL_ARGUMENT"],
			["early", "TOOL_NOT_ALLOWED"],
		]) {
			const result = context.messages.find(
				(item) => item.role === "toolResult" && item.toolCallId === id,
			);
			assert.ok(result?.role === "toolResult" && result.isError);
			assert.deepEqual(result.content, [{ type: "text", text: expected }]);
		}
	}
	if (scenario === "last-model-token-overage")
		return controlledResponse(
			model,
			[
				{
					type: "text",
					text: JSON.stringify({
						status: "needs_clarification",
						text: "超额正文不应展示",
					}),
				},
			],
			20_001,
		);
	if (scenario === "tool-model-token-overage")
		return controlledResponse(
			model,
			[
				{
					type: "toolCall",
					id: "overage_probe",
					name: "probe",
					arguments: { value: "sample" },
				},
			],
			20_001,
		);
	if (scenario === "fake-citation")
		return controlledResponse(model, [
			{
				type: "text",
				text: JSON.stringify({
					status: "answered",
					text: "禁止展示的伪造答复",
					evidenceIds: ["sample_1"],
				}),
			},
		]);
	const response = controlledModel(model, context, options);
	if (
		scenario === "commit-failure" &&
		context.messages.some(
			(item) => item.role === "toolResult" && item.toolCallId === "read_2",
		)
	) {
		return new Promise((resolve) => {
			process.once("SIGUSR2", () => resolve(response));
			process.stderr.write("COMMIT_WAITING\n");
		});
	}
	return response;
};

await startCli(process.env, async ({ cwd, sessionManager }) => {
	const modelRuntime = await ModelRuntime.create({
		credentials: new InMemoryCredentialStore(),
		modelsStore: new InMemoryModelsStore(),
		modelsPath: null,
		refreshOnCreate: false,
		allowModelNetwork: false,
	});
	// Pi SDK 合约需要认证标记；传输由本夹具替换，不发网络请求。
	await modelRuntime.setRuntimeApiKey("openai", "teyvatnotes-controlled-only");
	const staticModel = modelRuntime.getModels("openai")[0];
	assert.ok(staticModel, "Pi SDK 应可载入静态测试模型定义");
	const model = {
		...staticModel,
		contextWindow: maxConfiguredContextTokens,
		maxTokens: maxConfiguredOutputTokens,
	};
	const runtime = new PiAgentRuntime({
		cwd,
		model,
		modelRuntime,
		stream,
		sessionManager,
	});
	if (scenario.startsWith("guides-"))
		return {
			runtime,
			mode: "guide",
			data: guideData(),
			terminology: v2Terminology,
		};
	return {
		runtime,
		tools,
		...(scenario.startsWith("v2-")
			? {
					mode: "guide" as const,
					...(scenario === "v2-local-terminology"
						? {}
						: { terminology: v2Terminology }),
				}
			: { mode: "general" as const }),
	};
});
