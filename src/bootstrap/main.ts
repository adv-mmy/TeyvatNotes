#!/usr/bin/env node
import { realpathSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { PiSessionStore } from "../adapters/pi/session-store.js";
import { Clarification } from "../application/clarification.js";
import {
	createGuideTools,
	registeredTurnTools,
	type TurnTool,
} from "../application/guide-tools.js";
import type { AgentRuntime } from "../application/ports/agent.js";
import type { GuideData } from "../application/ports/guide-data.js";
import {
	GuideRetrieval,
	type RetrievalLimits,
} from "../application/retrieve-guides.js";
import { RunTurn } from "../application/run-turn.js";
import { type QuestionAvailability, runCli } from "../cli/runner.js";
import type { TerminologyEntry } from "../domain/query-scope.js";
import { readConfig, readTerminology, TerminologyError } from "./config.js";

type QuestionRuntimeFactory = (context: {
	readonly cwd: string;
	readonly sessionManager: PiSessionStore["managerFor"];
}) => Promise<
	{ readonly runtime: AgentRuntime } & (
		| ({
				readonly mode: "guide";
				readonly terminology?: readonly TerminologyEntry[];
		  } & (
				| {
						readonly data: GuideData;
						readonly retrievalLimits?: RetrievalLimits;
				  }
				| { readonly tools: readonly TurnTool[] }
		  ))
		| { readonly mode: "general"; readonly tools: readonly TurnTool[] }
	)
>;

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
	let availability: QuestionAvailability = {
		kind: "unavailable",
		code: "MODEL_NOT_CONFIGURED",
	};
	if (questions) {
		try {
			const terms =
				questions.mode === "guide"
					? (questions.terminology ?? (await readTerminology()))
					: undefined;
			availability = {
				kind: "ready",
				turn: new RunTurn(
					questions.runtime,
					config.limits,
					"data" in questions
						? createGuideTools(
								new GuideRetrieval(questions.data, questions.retrievalLimits),
							)
						: registeredTurnTools(questions.tools),
					(info) => {
						if (env.TEYVAT_DIAGNOSTICS === "1")
							process.stderr.write(`${JSON.stringify(info)}\n`);
					},
					new Clarification(terms),
				),
			};
		} catch (error) {
			if (!(error instanceof TerminologyError)) throw error;
			availability = { kind: "unavailable", code: error.code };
		}
	}
	await runCli(store, availability);
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
