import { resolve } from "node:path";
import type { TurnLimits } from "../application/turn-budget.js";

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

export const controlledContextWindow = 128_000;
export const controlledOutputWindow = 16_384;

export function readConfig(env: NodeJS.ProcessEnv): Config {
	const output = integer(
		env,
		"TEYVAT_MAX_OUTPUT_TOKENS",
		8192,
		1,
		controlledOutputWindow,
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
				controlledContextWindow,
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
