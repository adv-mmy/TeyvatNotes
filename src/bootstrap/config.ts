import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import type { EmbeddingConfig } from "../application/ports/guide-data.js";
import {
	type SourcePermit,
	validPermit,
} from "../application/ports/guide-source.js";
import type { TurnLimits } from "../application/turn-budget.js";
import {
	gameVersionKey,
	type TerminologyEntry,
} from "../domain/query-scope.js";

export interface Config {
	readonly sessionDirectory: string;
	readonly limits: TurnLimits;
	readonly currentGameVersion: string | null;
}

function integer(
	env: NodeJS.ProcessEnv,
	name: string,
	fallback: number,
	min: number,
	max: number,
) {
	const raw = env[name];
	if (raw === undefined) return fallback;
	const value = Number(raw);
	if (
		!/^\d+$/.test(raw) ||
		!Number.isSafeInteger(value) ||
		value < min ||
		value > max
	)
		throw new Error(`配置 ${name} 必须为 ${min}～${max} 的整数`);
	return value;
}

export const maxConfiguredContextTokens = 128_000;
export const maxConfiguredOutputTokens = 16_384;

export async function readWebConfig(env: NodeJS.ProcessEnv) {
	const path = env.TEYVAT_SOURCE_POLICY_FILE;
	if (!path) return undefined;
	const permits: unknown = JSON.parse(await readFile(path, "utf8"));
	if (
		!Array.isArray(permits) ||
		permits.length > 16 ||
		!permits.every(validPermit)
	)
		throw new Error("WEB_SOURCE_UNAVAILABLE");
	if (!permits.length) return undefined;
	return { permits: permits as readonly SourcePermit[] };
}

export async function readAnswerConfig(env: NodeJS.ProcessEnv) {
	const file = env.TEYVAT_MODEL_API_KEY_FILE ?? "/run/secrets/model_api_key";
	let key: string;
	try {
		key = (await readFile(file, "utf8")).trim();
	} catch (error) {
		if (
			env.TEYVAT_MODEL_API_KEY_FILE === undefined &&
			(error as NodeJS.ErrnoException).code === "ENOENT"
		)
			return undefined;
		throw new Error("MODEL_CONFIGURATION_INVALID");
	}
	const model = env.TEYVAT_MODEL_ID ?? "deepseek-flash";
	const url = new URL(env.TEYVAT_MODEL_BASE_URL ?? "https://api.deepseek.com");
	if (
		!/^[\x21-\x7e]+$/u.test(key) ||
		!/^[\w./:-]{1,128}$/u.test(model) ||
		url.protocol !== "https:" ||
		url.username ||
		url.password ||
		url.search ||
		url.hash
	)
		throw new Error("MODEL_CONFIGURATION_INVALID");
	return { key, model, baseUrl: url.href.replace(/\/+$/u, "") };
}

export class TerminologyError extends Error {
	constructor(
		readonly code: "TERMINOLOGY_UNAVAILABLE" | "TERMINOLOGY_INVALID",
	) {
		super(code);
	}
}

function record(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonempty(value: unknown): value is string {
	return typeof value === "string" && value.trim() !== "";
}

export function parseTerminology(value: unknown): readonly TerminologyEntry[] {
	const invalid = () => new TerminologyError("TERMINOLOGY_INVALID");
	if (
		!record(value) ||
		!nonempty(value.revision) ||
		!Array.isArray(value.terms)
	)
		throw invalid();
	const seen = new Set<string>();
	const terms: TerminologyEntry[] = [];
	for (const item of value.terms as unknown[]) {
		if (
			!record(item) ||
			!nonempty(item.name) ||
			(item.category !== "character" &&
				item.category !== "weapon" &&
				item.category !== "artifact") ||
			!Array.isArray(item.aliases) ||
			!item.aliases.every(nonempty)
		)
			throw invalid();
		const name = item.name.trim();
		const key = `${item.category}:${name.normalize("NFKC").toLowerCase()}`;
		if (seen.has(key)) throw invalid();
		seen.add(key);
		terms.push({
			category: item.category,
			name,
			aliases: item.aliases.map((alias) => alias.trim()),
		});
	}
	if (!terms.some((term) => term.category === "character")) throw invalid();
	return terms;
}

export async function readTerminology(
	location = new URL(
		"../../../resources/terminology/terminology.json",
		import.meta.url,
	),
): Promise<readonly TerminologyEntry[]> {
	let raw: string;
	try {
		raw = await readFile(location, "utf8");
	} catch {
		throw new TerminologyError("TERMINOLOGY_UNAVAILABLE");
	}
	let value: unknown;
	try {
		value = JSON.parse(raw);
	} catch {
		throw new TerminologyError("TERMINOLOGY_INVALID");
	}
	return parseTerminology(value);
}

export function readConfig(env: NodeJS.ProcessEnv): Config {
	const currentGameVersion = env.TEYVAT_CURRENT_GAME_VERSION?.trim() || null;
	if (
		currentGameVersion !== null &&
		gameVersionKey(currentGameVersion) === undefined
	)
		throw new Error(
			"配置 TEYVAT_CURRENT_GAME_VERSION 必须是已核实的游戏版本号",
		);
	const output = integer(
		env,
		"TEYVAT_MAX_OUTPUT_TOKENS",
		8192,
		1,
		maxConfiguredOutputTokens,
	);
	return {
		sessionDirectory: resolve(env.TEYVAT_SESSION_DIR ?? "/data/sessions"),
		currentGameVersion,
		limits: {
			turnMs: integer(env, "TEYVAT_TURN_MS", 60_000, 1, 300_000),
			modelMs: integer(env, "TEYVAT_MODEL_MS", 20_000, 1, 120_000),
			toolMs: integer(env, "TEYVAT_TOOL_MS", 30_000, 1, 60_000),
			searchMs: integer(env, "TEYVAT_SEARCH_MS", 8_000, 1, 60_000),
			fetchMs: integer(env, "TEYVAT_FETCH_MS", 8_000, 1, 60_000),
			modelCalls: integer(env, "TEYVAT_MODEL_CALLS", 16, 1, 30),
			toolCalls: integer(env, "TEYVAT_TOOL_CALLS", 8, 1, 100),
			searchCalls: integer(env, "TEYVAT_SEARCH_CALLS", 1, 0, 1),
			fetchCalls: integer(env, "TEYVAT_FETCH_CALLS", 3, 0, 6),
			contextTokens: integer(
				env,
				"TEYVAT_CONTEXT_TOKENS",
				65_536,
				1,
				maxConfiguredContextTokens,
			),
			cumulativeTokens: integer(
				env,
				"TEYVAT_CUMULATIVE_TOKENS",
				180_000,
				1,
				2_000_000,
			),
			maxOutputTokens: output,
		},
	};
}

function origin(env: NodeJS.ProcessEnv, name: string, fallback: string): URL {
	try {
		const url = new URL(env[name] ?? fallback);
		if (
			(url.protocol === "http:" || url.protocol === "https:") &&
			url.pathname === "/" &&
			!url.username &&
			!url.password &&
			!url.search &&
			!url.hash
		)
			return url;
	} catch {}
	throw new Error(`配置 ${name} 必须是无凭据的 HTTP(S) 服务地址`);
}

export function readGuideStorageConfig(env: NodeJS.ProcessEnv) {
	const model = env.TEYVAT_EMBEDDING_MODEL ?? "qwen3-embedding:0.6b";
	if (!/^qwen3-embedding:[\w.-]+$/u.test(model))
		throw new Error(
			"配置 TEYVAT_EMBEDDING_MODEL 必须指定 qwen3-embedding 的模型标签",
		);
	const dimensions = integer(
		env,
		"TEYVAT_EMBEDDING_DIMENSIONS",
		1024,
		32,
		4096,
	);
	const id = createHash("sha256")
		.update(JSON.stringify([model, dimensions]))
		.digest("hex")
		.slice(0, 24);
	const embedding: EmbeddingConfig = { id, model, dimensions };
	const collection = env.TEYVAT_CHROMA_COLLECTION ?? "teyvatnotes_guides";
	if (!/^[a-z0-9][a-z0-9_-]{1,31}$/u.test(collection))
		throw new Error("配置 TEYVAT_CHROMA_COLLECTION 不是有效的集合前缀");
	return {
		embedding,
		collection,
		chroma: origin(env, "TEYVAT_CHROMA_URL", "http://chromadb:8000"),
		ollama: origin(env, "TEYVAT_OLLAMA_URL", "http://ollama:11434"),
	};
}

export async function readDatabaseConfig(env: NodeJS.ProcessEnv) {
	let password: string;
	try {
		password = (
			await readFile(
				env.TEYVAT_DB_PASSWORD_FILE ?? "/run/secrets/postgres_password",
				"utf8",
			)
		).replace(/[\r\n]+$/u, "");
		if (!password.trim()) throw new Error();
	} catch {
		throw new Error(
			"配置 TEYVAT_DB_PASSWORD_FILE 未指向可读取的 PostgreSQL 密钥文件",
		);
	}
	return {
		host: env.TEYVAT_DB_HOST ?? "postgres",
		port: integer(env, "TEYVAT_DB_PORT", 5432, 1, 65535),
		user: env.TEYVAT_DB_USER ?? "teyvatnotes",
		database: env.TEYVAT_DB_NAME ?? "teyvatnotes",
		password,
		max: 4,
		connectionTimeoutMillis: 3000,
		statement_timeout: 4000,
		idle_in_transaction_session_timeout: 6000,
	};
}
