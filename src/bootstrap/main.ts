#!/usr/bin/env node
import { realpathSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { PiSessionStore } from "../adapters/pi/session-store.js";
import { Clarification } from "../application/clarification.js";
import type { AgentRuntime } from "../application/ports/agent.js";
import { RunTurn, type TurnTool } from "../application/run-turn.js";
import { runCli } from "../cli/runner.js";
import type { CharacterTerm } from "../domain/query-scope.js";
import { readConfig } from "./config.js";

type QuestionRuntimeFactory = (context: {
	readonly cwd: string;
	readonly sessionManager: PiSessionStore["managerFor"];
}) => Promise<{
	readonly runtime: AgentRuntime;
	readonly tools: readonly TurnTool[];
	readonly terminology?: readonly CharacterTerm[];
}>;

export async function startCli(
	env: NodeJS.ProcessEnv = process.env,
	createQuestionRuntime?: QuestionRuntimeFactory,
) {
	const config = readConfig(env);
	const cwd = process.cwd();
	const store = new PiSessionStore(config.sessionDirectory, cwd);
	const questions = await createQuestionRuntime?.({
		cwd,
		sessionManager: (turnId) => store.managerFor(turnId),
	});
	// 当前阶段没有装配正式的问答依赖；会话和历史不依赖模型。
	const turn = questions
		? new RunTurn(
				questions.runtime,
				config.limits,
				questions.tools,
				(info) => {
					if (env.TEYVAT_DIAGNOSTICS === "1")
						process.stderr.write(`${JSON.stringify(info)}\n`);
				},
				new Clarification(questions.terminology),
			)
		: undefined;
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
