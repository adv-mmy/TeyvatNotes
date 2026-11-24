import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import type { TurnLimits } from "../application/turn-budget.js";
import type { TerminologyEntry } from "../domain/query-scope.js";

export interface Config {
	readonly sessionDirectory: string;
	readonly limits: TurnLimits;
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
	const output = integer(
		env,
		"TEYVAT_MAX_OUTPUT_TOKENS",
		8192,
		1,
		maxConfiguredOutputTokens,
	);
	return {
		sessionDirectory: resolve(env.TEYVAT_SESSION_DIR ?? "/data/sessions"),
		limits: {
			turnMs: integer(env, "TEYVAT_TURN_MS", 30_000, 1, 300_000),
			modelMs: integer(env, "TEYVAT_MODEL_MS", 10_000, 1, 120_000),
			toolMs: integer(env, "TEYVAT_TOOL_MS", 3_000, 1, 60_000),
			modelCalls: integer(env, "TEYVAT_MODEL_CALLS", 6, 1, 30),
			toolCalls: integer(env, "TEYVAT_TOOL_CALLS", 8, 1, 100),
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
