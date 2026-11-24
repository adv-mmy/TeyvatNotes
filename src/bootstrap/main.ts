#!/usr/bin/env node
import { realpathSync } from "node:fs";
import { pathToFileURL } from "node:url";
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
import { PiAgentRuntime } from "../adapters/pi/runtime.js";
import { PiSessionStore } from "../adapters/pi/session-store.js";
import {
	type ControlledToolExecutor,
	RunTurn,
} from "../application/run-turn.js";
import { runCli } from "../cli/runner.js";
import {
	controlledContextWindow,
	controlledOutputWindow,
	readConfig,
} from "./config.js";

export function controlledResponse(
	model: Model<Api>,
	content: AssistantMessage["content"],
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

export const controlledModel: StreamFn = (model, context) => {
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

export async function startCli(
	env: NodeJS.ProcessEnv = process.env,
	stream: StreamFn = controlledModel,
	tool?: ControlledToolExecutor,
) {
	const config = readConfig(env);
	const cwd = process.cwd();
	const modelRuntime = await ModelRuntime.create({
		credentials: new InMemoryCredentialStore(),
		modelsStore: new InMemoryModelsStore(),
		modelsPath: null,
		refreshOnCreate: false,
		allowModelNetwork: false,
	});
	// SDK 合约要求一个认证标记。此处不读取真实密钥或调用网络。
	await modelRuntime.setRuntimeApiKey("openai", "teyvatnotes-controlled-only");
	const staticModel = modelRuntime.getModels("openai")[0];
	if (!staticModel) throw new Error("Pi 静态测试模型未加载");
	const model: Model<Api> = {
		...staticModel,
		contextWindow: controlledContextWindow,
		maxTokens: controlledOutputWindow,
	};
	const store = new PiSessionStore(config.sessionDirectory, cwd);
	const runtime = new PiAgentRuntime({
		cwd,
		model,
		modelRuntime,
		stream,
		sessionManager: (turnId) => store.managerFor(turnId),
	});
	const turn = new RunTurn(runtime, config.limits, tool, (info) => {
		if (env.TEYVAT_DIAGNOSTICS === "1")
			process.stderr.write(`${JSON.stringify(info)}\n`);
	});
	await runCli(store, turn);
}

if (
	process.argv[1] &&
	import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href
)
	await startCli().catch((error: unknown) => {
		console.error(
			error instanceof Error && error.message.startsWith("配置 TEYVAT_")
				? error.message
				: "CLI_START_FAILED：无法启动 TeyvatNotes。",
		);
		process.exitCode = 1;
	});
