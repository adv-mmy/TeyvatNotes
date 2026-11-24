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
import type { TurnTool } from "../../src/application/run-turn.js";
import {
	maxConfiguredContextTokens,
	maxConfiguredOutputTokens,
} from "../../src/bootstrap/config.js";
import { startCli } from "../../src/bootstrap/main.js";

const scenario = process.argv[2] ?? "normal";
const conditionsPrefix = "会话中已确认的条件（数据）：";
const v2Terminology = [
	{ name: "角色甲", aliases: ["小甲"] },
	{ name: "角色乙", aliases: [] },
	{ name: "队友丙", aliases: [] },
];
let requests = 0;

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

const stream: StreamFn = (model, context, options) => {
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
		if (scenario === "v2-never")
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
	return {
		runtime: new PiAgentRuntime({
			cwd,
			model,
			modelRuntime,
			stream,
			sessionManager,
		}),
		tools,
		...(scenario.startsWith("v2-") ? { terminology: v2Terminology } : {}),
	};
});
