import assert from "node:assert/strict";
import { test } from "node:test";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import {
	type Api,
	type AssistantMessage,
	createAssistantMessageEventStream,
	InMemoryCredentialStore,
	InMemoryModelsStore,
	type Model,
	type ToolCall as PiToolCall,
	type TranscriptContext,
} from "@earendil-works/pi-ai";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { PiAgentRuntime } from "../../src/adapters/pi/runtime.js";
import type {
	AgentRunHost,
	ToolBatch,
	ToolDefinition,
	ToolResult,
} from "../../src/application/ports/agent.js";

const tools = ["probe", "read_candidate", "report_gap"].map(
	(name): ToolDefinition => ({
		name,
		description: "受控集成测试工具",
		parameters: {
			type: "object",
			properties: { value: { type: "string" } },
			required: ["value"],
			additionalProperties: false,
		},
	}),
);

const ask = { prompt: "确认两步工具循环", tools };
const freshSignal = () => new AbortController().signal;
const use = (id: string, name: string, value: string | number): PiToolCall => ({
	type: "toolCall",
	id,
	name,
	arguments: { value },
});
const say = (text: string): AssistantMessage["content"] => [
	{ type: "text", text },
];

function answer(
	model: Model<Api>,
	content: AssistantMessage["content"],
	stopReason: "toolUse" | "stop" | "length" = content.some(
		(item) => item.type === "toolCall",
	)
		? "toolUse"
		: "stop",
): ReturnType<typeof createAssistantMessageEventStream> {
	const stream = createAssistantMessageEventStream();
	stream.push({
		type: "done",
		reason: stopReason,
		message: {
			role: "assistant",
			api: model.api,
			provider: model.provider,
			model: model.id,
			stopReason,
			content,
			timestamp: Date.now(),
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
		},
	});
	stream.end();
	return stream;
}

async function setup(stream: StreamFn) {
	const modelRuntime = await ModelRuntime.create({
		credentials: new InMemoryCredentialStore(),
		modelsStore: new InMemoryModelsStore(),
		modelsPath: null,
		refreshOnCreate: false,
		allowModelNetwork: false,
	});
	// SDK 的提示词入口需要已配置认证；该值仅在内存，传输由测试完全替代。
	await modelRuntime.setRuntimeApiKey("openai", "teyvatnotes-test-only");
	const model = modelRuntime.getModels("openai")[0];
	assert.ok(model, "Pi SDK 应可载入静态测试模型定义");
	return new PiAgentRuntime({
		model,
		modelRuntime,
		stream,
		cwd: process.cwd(),
	});
}

function delivered(context: TranscriptContext) {
	return context.messages.filter((message) => message.role === "toolResult");
}

function content(message: ReturnType<typeof delivered>[number] | undefined) {
	const entry = message?.content[0];
	return entry?.type === "text" ? entry.text : undefined;
}

function assertDeclaredTools(context: TranscriptContext) {
	const names = context.messages.flatMap((message) =>
		message.role === "system"
			? (message.toolsAdded ?? []).map((tool) => tool.name)
			: [],
	);
	assert.deepEqual(
		[...new Set(names)].sort(),
		tools.map((tool) => tool.name).sort(),
	);
}

for (const hit of [true, false]) {
	test(`真实 Pi 循环根据工具结果选择${hit ? "回读" : "报告缺口"}`, {
		timeout: 10_000,
	}, async () => {
		let actualRequests = 0;
		const approvedDrafts: string[] = [];
		const executed: string[] = [];
		const runtime = await setup((model, context) => {
			assert.equal(approvedDrafts[actualRequests], JSON.stringify(context));
			assertDeclaredTools(context);
			actualRequests++;
			const results = delivered(context);
			const probed = results.find((result) => result.toolCallId === "probe_1");
			if (!probed) return answer(model, [use("probe_1", "probe", "topic")]);
			const decision = JSON.parse(content(probed) ?? "");
			const next = decision.hit ? "read_candidate" : "report_gap";
			const second = results.find((result) => result.toolCallId === "step_2");
			if (!second) return answer(model, [use("step_2", next, decision.value)]);
			assert.equal(second.toolName, next);
			assert.equal(second.isError, false);
			return answer(model, say(JSON.parse(content(second) ?? "").summary));
		});
		const host: AgentRunHost = {
			async prepareModelRequest(draft) {
				assert.equal(draft.signal.aborted, false);
				approvedDrafts.push(draft.serializedContext);
				return { allowed: true };
			},
			async executeToolBatch(batch) {
				assert.equal(batch.rejected.length, 0);
				return batch.calls.map((call) => {
					executed.push(call.name);
					return {
						id: call.id,
						ok: true,
						content:
							call.name === "probe"
								? { hit, value: hit ? "candidate_1" : "missing" }
								: {
										summary:
											call.name === "read_candidate" ? "已回读" : "无候选",
									},
					};
				});
			},
		};
		assert.deepEqual(await runtime.run(ask, host, freshSignal()), {
			status: "completed",
			text: hit ? "已回读" : "无候选",
		});
		assert.deepEqual(executed, [
			"probe",
			hit ? "read_candidate" : "report_gap",
		]);
		assert.equal(approvedDrafts.length, 3);
		assert.equal(actualRequests, 3);
	});
}

test("同批授权只检查一次：未登记工具、非法参数与尚未授予的回读都没有副作用", {
	timeout: 10_000,
}, async () => {
	let actualRequests = 0;
	const executed: string[] = [];
	const batches: ToolBatch[] = [];
	const runtime = await setup((model, context) => {
		actualRequests++;
		if (actualRequests === 1) {
			assertDeclaredTools(context);
			return answer(model, [
				use("probe", "probe", "topic"),
				use("early_read", "read_candidate", "future_candidate"),
				use("not_allowed", "bash", "must-not-run"),
				use("bad_args", "report_gap", 42),
			]);
		}
		assert.deepEqual(
			delivered(context).map((result) => [
				result.toolCallId,
				result.isError,
				content(result),
			]),
			[
				["probe", false, '"future_candidate"'],
				["early_read", true, "TOOL_NOT_ALLOWED"],
				["not_allowed", true, "TOOL_NOT_ALLOWED"],
				["bad_args", true, "INVALID_TOOL_ARGUMENT"],
			],
		);
		return answer(model, say("拒绝结果已回送"));
	});
	const host: AgentRunHost = {
		async prepareModelRequest() {
			return { allowed: true };
		},
		async executeToolBatch(batch) {
			batches.push(batch);
			assert.deepEqual(
				batch.rejected.map(({ id, code }) => [id, code]),
				[
					["not_allowed", "TOOL_NOT_ALLOWED"],
					["bad_args", "INVALID_TOOL_ARGUMENT"],
				],
			);
			return batch.calls
				.map<ToolResult>((call) => {
					if (call.name !== "probe")
						return { id: call.id, ok: false, code: "TOOL_NOT_ALLOWED" };
					executed.push(call.name);
					return { id: call.id, ok: true, content: "future_candidate" };
				})
				.reverse(); // 结果必须按 ID 配对，而不是按应用返回顺序。
		},
	};
	assert.deepEqual(await runtime.run(ask, host, freshSignal()), {
		status: "completed",
		text: "拒绝结果已回送",
	});
	assert.deepEqual(executed, ["probe"]);
	assert.equal(batches.length, 1);
	assert.equal(actualRequests, 2);
});

const unexpectedTool: AgentRunHost["executeToolBatch"] = async () => {
	assert.fail("不应开始执行任何工具");
};

test("整批只有禁用工具时仍得到关联的稳定拒绝", {
	timeout: 10_000,
}, async () => {
	let requests = 0;
	let batches = 0;
	const runtime = await setup((model, context) => {
		requests++;
		if (requests === 1)
			return answer(model, [use("blocked", "bash", "not executed")]);
		const [result] = delivered(context);
		assert.equal(result?.toolCallId, "blocked");
		assert.equal(result?.isError, true);
		assert.equal(content(result), "TOOL_NOT_ALLOWED");
		return answer(model, say("工具未执行"));
	});
	const result = await runtime.run(
		ask,
		{
			async prepareModelRequest() {
				return { allowed: true };
			},
			async executeToolBatch(batch) {
				batches++;
				assert.deepEqual(batch.calls, []);
				assert.deepEqual(batch.rejected, [
					{ id: "blocked", name: "bash", code: "TOOL_NOT_ALLOWED" },
				]);
				return [];
			},
		},
		freshSignal(),
	);
	assert.deepEqual(result, { status: "completed", text: "工具未执行" });
	assert.equal(requests, 2);
	assert.equal(batches, 1);
});

test("空白输入不进入 SDK 模型或工具入口", { timeout: 10_000 }, async () => {
	const runtime = await setup(() => assert.fail("空白输入不能触发模型传输"));
	const result = await runtime.run(
		{ ...ask, prompt: " \t " },
		{
			async prepareModelRequest() {
				assert.fail("不应启动模型请求检查");
			},
			executeToolBatch: unexpectedTool,
		},
		freshSignal(),
	);
	assert.deepEqual(result, { status: "failed", code: "INVALID_INPUT" });
});

for (const [scenario, code] of [
	["duplicate", "DUPLICATE_TOOL_CALL"],
	["truncation", "MODEL_OUTPUT_LIMIT"],
] as const) {
	test(`模型异常 ${scenario} 停止本轮且不执行工具`, {
		timeout: 10_000,
	}, async () => {
		let actualRequests = 0;
		let toolDispatches = 0;
		const runtime = await setup((model) => {
			actualRequests++;
			const call = use("repeat", "probe", "test");
			return scenario === "duplicate"
				? answer(model, [call, call])
				: answer(model, [call], "length");
		});
		assert.deepEqual(
			await runtime.run(
				ask,
				{
					async prepareModelRequest() {
						return { allowed: true };
					},
					async executeToolBatch(batch) {
						toolDispatches++;
						return unexpectedTool(batch);
					},
				},
				freshSignal(),
			),
			{ status: "failed", code },
		);
		assert.equal(actualRequests, 1);
		assert.equal(toolDispatches, 0);
	});
}

test("第二次真实请求前被应用预算拒绝，不会自动重试", {
	timeout: 10_000,
}, async () => {
	let gates = 0;
	let actualRequests = 0;
	const runtime = await setup((model) => {
		actualRequests++;
		return answer(model, [use("only", "probe", "test")]);
	});
	const result = await runtime.run(
		ask,
		{
			async prepareModelRequest() {
				gates++;
				return gates === 1
					? { allowed: true }
					: { allowed: false, code: "BUDGET_EXHAUSTED" };
			},
			async executeToolBatch(batch) {
				return batch.calls.map((call) => ({
					id: call.id,
					ok: true,
					content: "test",
				}));
			},
		},
		freshSignal(),
	);
	assert.deepEqual(result, { status: "failed", code: "BUDGET_EXHAUSTED" });
	assert.equal(gates, 2);
	assert.equal(actualRequests, 1);
});

test("取消后停止，不会执行模型随后给出的工具", {
	timeout: 10_000,
}, async () => {
	const controller = new AbortController();
	let requests = 0;
	let batches = 0;
	const runtime = await setup((model, _context, options) => {
		requests++;
		assert.equal(options?.signal?.aborted, false);
		controller.abort();
		assert.equal(options?.signal?.aborted, true);
		return answer(model, [use("late", "probe", "must-not-run")]);
	});
	assert.deepEqual(
		await runtime.run(
			ask,
			{
				async prepareModelRequest() {
					return { allowed: true };
				},
				async executeToolBatch(batch) {
					batches++;
					return unexpectedTool(batch);
				},
			},
			controller.signal,
		),
		{ status: "cancelled" },
	);
	assert.equal(requests, 1);
	assert.equal(batches, 0);
});

test("拒绝错配的工具结果 ID，且不发起下一次模型请求", {
	timeout: 10_000,
}, async () => {
	let requests = 0;
	const runtime = await setup((model) => {
		requests++;
		return answer(model, [use("expected", "probe", "test")]);
	});
	assert.deepEqual(
		await runtime.run(
			ask,
			{
				async prepareModelRequest() {
					return { allowed: true };
				},
				async executeToolBatch() {
					return [{ id: "wrong", ok: true, content: "test" }];
				},
			},
			freshSignal(),
		),
		{ status: "failed", code: "INVALID_TOOL_RESULT" },
	);
	assert.equal(requests, 1);
});
