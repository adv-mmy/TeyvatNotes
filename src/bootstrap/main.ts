#!/usr/bin/env node
import { realpathSync } from "node:fs";
import { open } from "node:fs/promises";
import { extname } from "node:path";
import { pathToFileURL } from "node:url";
import pg from "pg";
import { ChromaVectorIndex } from "../adapters/chroma/vector-index.js";
import { OllamaEmbedder } from "../adapters/ollama/embedder.js";
import { createResponsesRuntime } from "../adapters/pi/runtime.js";
import { PiSessionStore } from "../adapters/pi/session-store.js";
import { PostgreSqlGuideRepository } from "../adapters/postgres/guide-repository.js";
import { migrate } from "../adapters/postgres/migrate.js";
import { DeepSeekSource } from "../adapters/web/deepseek-source.js";
import { parseWebGuide } from "../adapters/web/metadata-parser.js";
import { Clarification } from "../application/clarification.js";
import {
	createGuideTools,
	registeredTurnTools,
	type TurnTool,
	TurnToolError,
} from "../application/guide-tools.js";
import {
	IngestError,
	IngestGuide,
	prepareGuide,
	webSupplement,
} from "../application/ingest-guide.js";
import type { AgentRuntime } from "../application/ports/agent.js";
import type { GuideData } from "../application/ports/guide-data.js";
import type { WebSupplement } from "../application/ports/guide-source.js";
import {
	GuideRetrieval,
	type RetrievalLimits,
} from "../application/retrieve-guides.js";
import { RunTurn } from "../application/run-turn.js";
import {
	maintenanceMessage,
	parseMaintenance,
	type QuestionAvailability,
	runCli,
} from "../cli/runner.js";
import type { TerminologyEntry } from "../domain/query-scope.js";
import {
	readAnswerConfig,
	readConfig,
	readDatabaseConfig,
	readGuideStorageConfig,
	readTerminology,
	readWebConfig,
	TerminologyError,
} from "./config.js";

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
						readonly supplement?: WebSupplement;
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
	io: { input?: NodeJS.ReadableStream; output?: NodeJS.WritableStream } = {},
) {
	const config = readConfig(env);
	const cwd = process.cwd();
	const store = new PiSessionStore(config.sessionDirectory, cwd);
	const context: Parameters<QuestionRuntimeFactory>[0] = {
		cwd,
		sessionManager: (turnId) => store.managerFor(turnId),
	};
	let pool: pg.Pool | undefined;
	let availability: QuestionAvailability = {
		kind: "unavailable",
		code: "MODEL_NOT_CONFIGURED",
	};
	try {
		let questions = await createQuestionRuntime?.(context);
		if (!createQuestionRuntime) {
			const answer = await readAnswerConfig(env);
			if (answer) {
				availability = {
					kind: "unavailable",
					code: "GUIDE_STORAGE_UNAVAILABLE",
				};
				pool = new pg.Pool(await readDatabaseConfig(env));
				const data = createGuideStorage(pool, env);
				availability = {
					kind: "unavailable",
					code: "MODEL_CONFIGURATION_INVALID",
				};
				const runtime = await createResponsesRuntime({
					...context,
					...answer,
					contextWindow: config.limits.contextTokens,
					maxTokens: config.limits.maxOutputTokens,
				});
				const terminology = await readTerminology();
				const ingest = new IngestGuide(
					data.repository,
					data.embeddings,
					data.vectorIndex,
					data.config,
				);
				questions = {
					runtime,
					data,
					terminology,
					mode: "guide",
					supplement: async (scope, budget, signal) => {
						const config = await readWebConfig(env).catch(() => {
							throw new TurnToolError("WEB_SOURCE_UNAVAILABLE");
						});
						if (!config) return "source_restricted";
						return webSupplement({
							source: new DeepSeekSource(config.key),
							permits: config.permits,
							terms: terminology,
							parse: parseWebGuide,
							ingest,
							archive: (page, s) => data.repository.archiveIncomplete(page, s),
						})(scope, budget, signal);
					},
				};
			}
		}
		if (questions) {
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
								questions.supplement,
							)
						: registeredTurnTools(questions.tools),
					(info) => {
						if (env.TEYVAT_DIAGNOSTICS === "1")
							process.stderr.write(`${JSON.stringify(info)}\n`);
					},
					new Clarification(terms),
					terms,
				),
			};
		}
	} catch (error) {
		if (error instanceof TerminologyError)
			availability = { kind: "unavailable", code: error.code };
		else if (createQuestionRuntime) throw error;
		else if (
			availability.kind !== "unavailable" ||
			availability.code !== "GUIDE_STORAGE_UNAVAILABLE"
		)
			availability = {
				kind: "unavailable",
				code: "MODEL_CONFIGURATION_INVALID",
			};
	}
	try {
		await runCli(store, availability, io.input, io.output);
	} finally {
		await pool?.end();
	}
}

function createGuideStorage(pool: pg.Pool, env: NodeJS.ProcessEnv) {
	const config = readGuideStorageConfig(env);
	return {
		config: config.embedding,
		repository: new PostgreSqlGuideRepository(pool, config.embedding.id),
		embeddings: new OllamaEmbedder(config.ollama, config.embedding),
		vectorIndex: new ChromaVectorIndex(
			config.chroma,
			config.collection,
			config.embedding,
		),
	};
}

async function readImport(file: string, signal: AbortSignal) {
	const ext = extname(file).toLowerCase();
	if (ext !== ".json" && ext !== ".md" && ext !== ".markdown")
		throw new IngestError("GUIDE_INVALID");
	let raw: string;
	try {
		const handle = await open(file, "r");
		try {
			const stat = await handle.stat();
			if (!stat.isFile() || stat.size > 1_048_576) throw new Error();
			const buffer = Buffer.alloc(stat.size + 1);
			let offset = 0;
			while (offset < buffer.length) {
				signal.throwIfAborted();
				const { bytesRead } = await handle.read(
					buffer,
					offset,
					buffer.length - offset,
				);
				if (!bytesRead) break;
				offset += bytesRead;
			}
			if (offset !== stat.size) throw new Error();
			raw = new TextDecoder("utf-8", { fatal: true }).decode(
				buffer.subarray(0, offset),
			);
		} finally {
			await handle.close();
		}
	} catch {
		signal.throwIfAborted();
		throw new IngestError("GUIDE_INVALID");
	}
	return prepareGuide(
		raw,
		ext === ".json" ? "json" : "markdown",
		await readTerminology(),
	);
}

async function startMaintenance(args: readonly string[], env = process.env) {
	const command = parseMaintenance(args);
	if (!command) {
		console.error(maintenanceMessage("MAINTENANCE_USAGE"));
		process.exitCode = 2;
		return;
	}
	const controller = new AbortController();
	const cancel = () => controller.abort();
	process.once("SIGINT", cancel);
	process.once("SIGTERM", cancel);
	const signal = AbortSignal.any([
		controller.signal,
		AbortSignal.timeout(180_000),
	]);
	let pool: pg.Pool | undefined;
	try {
		const document =
			command.kind === "import"
				? await readImport(command.file, signal)
				: undefined;
		pool = new pg.Pool(await readDatabaseConfig(env));
		if (command.kind === "migrate") {
			const applied = await migrate(pool, signal);
			console.log(
				applied ? "已初始化攻略库。" : "攻略库迁移已执行，无需重复更新。",
			);
			return;
		}
		const { repository, embeddings, vectorIndex, config } = createGuideStorage(
			pool,
			env,
		);
		const ingest = new IngestGuide(repository, embeddings, vectorIndex, config);
		if (command.kind === "publish") {
			await ingest.publish(command.id, signal);
			console.log(`已发布攻略：${command.id}`);
		} else if (document) {
			const result = await ingest.import(document, command.publish, signal);
			console.log(
				`${result.published ? "已发布" : "待发布"}攻略：${result.id}`,
			);
		}
	} catch (error) {
		console.error(
			error instanceof Error && error.message.startsWith("配置 TEYVAT_")
				? error.message
				: maintenanceMessage(
						signal.aborted
							? "MAINTENANCE_TIMEOUT"
							: error instanceof IngestError
								? error.code
								: error instanceof Error &&
										error.message === "MIGRATION_CHECKSUM_MISMATCH"
									? "MIGRATION_CHECKSUM_MISMATCH"
									: "GUIDE_STORAGE_UNAVAILABLE",
					),
		);
		process.exitCode = 1;
	} finally {
		try {
			await pool?.end();
		} finally {
			process.removeListener("SIGINT", cancel);
			process.removeListener("SIGTERM", cancel);
		}
	}
}

if (
	process.argv[1] &&
	import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href
)
	await (process.argv.length > 2
		? startMaintenance(process.argv.slice(2))
		: startCli()
	).catch((error: unknown) => {
		console.error(
			error instanceof Error && error.message.startsWith("配置 TEYVAT_")
				? error.message
				: "CLI_START_FAILED：无法启动 TeyvatNotes。",
		);
		process.exitCode = 1;
	});
