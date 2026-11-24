import assert from "node:assert/strict";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import { executeControlledTool } from "../../src/application/run-turn.js";
import {
	controlledModel,
	controlledResponse,
	startCli,
} from "../../src/bootstrap/main.js";

const scenario = process.argv[2] ?? "normal";
const conditionsPrefix = "会话中已确认的条件（数据）：";
let requests = 0;

const stream: StreamFn = (model, context, options) => {
	requests++;
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

await startCli(process.env, stream, async (action, value, signal) => {
	if (scenario === "tool-error") throw new Error("未公开的工具原文");
	if (scenario === "tool-timeout") return new Promise(() => {});
	return executeControlledTool(action, value, signal);
});
