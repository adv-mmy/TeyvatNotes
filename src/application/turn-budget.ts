import type { ModelRequestDraft, PreparedModelRequest } from "./ports/agent.js";

export interface TurnLimits {
	readonly turnMs: number;
	readonly modelMs: number;
	readonly toolMs: number;
	readonly modelCalls: number;
	readonly toolCalls: number;
	readonly contextTokens: number;
	readonly cumulativeTokens: number;
	readonly maxOutputTokens: number;
}

/** 不依赖具体供应商的估算，以 UTF-8 字节为保守上界，外加协议余量。 */
export class TurnBudget {
	readonly deadline: AbortSignal;
	readonly signal: AbortSignal;
	private readonly timer: NodeJS.Timeout;
	private models = 0;
	private tools = 0;
	private rejected = 0;
	private estimatedTokens = 0;
	private actualTokens = 0;
	private accountedTokens = 0;
	// Pi 逐次等待模型结果；没有返回有效用量时，保留该次请求的预留。
	private pendingEstimate: number | undefined;

	constructor(
		readonly limits: TurnLimits,
		parent: AbortSignal,
	) {
		const deadline = new AbortController();
		this.timer = setTimeout(() => deadline.abort(), limits.turnMs);
		this.deadline = deadline.signal;
		this.signal = AbortSignal.any([parent, this.deadline]);
	}

	close(): void {
		clearTimeout(this.timer);
	}

	prepare(draft: ModelRequestDraft): PreparedModelRequest {
		this.signal.throwIfAborted();
		draft.signal.throwIfAborted();
		const estimate =
			Buffer.byteLength(draft.serializedContext, "utf8") +
			128 +
			this.limits.maxOutputTokens;
		if (estimate > this.limits.contextTokens)
			return { allowed: false, code: "CONTEXT_WINDOW_EXCEEDED" };
		if (
			this.models >= this.limits.modelCalls ||
			this.accountedTokens + estimate > this.limits.cumulativeTokens
		)
			return { allowed: false, code: "BUDGET_EXHAUSTED" };
		this.models++;
		this.estimatedTokens += estimate;
		this.accountedTokens += estimate;
		this.pendingEstimate = estimate;
		return {
			allowed: true,
			maxOutputTokens: this.limits.maxOutputTokens,
			deadline: AbortSignal.timeout(this.limits.modelMs),
		};
	}

	recordUsage(tokens: number): boolean {
		const estimate = this.pendingEstimate;
		this.pendingEstimate = undefined;
		if (Number.isSafeInteger(tokens) && tokens >= 0) {
			this.actualTokens += tokens;
			// 逐次结算：其他请求的低用量不能抵消本次超出预留的部分。
			this.accountedTokens += Math.max(0, tokens - (estimate ?? 0));
		}
		return this.accountedTokens <= this.limits.cumulativeTokens;
	}

	startTool(): AbortSignal | undefined {
		this.signal.throwIfAborted();
		if (this.tools >= this.limits.toolCalls) return undefined;
		this.tools++;
		return AbortSignal.timeout(this.limits.toolMs);
	}

	recordRejection(count = 1): void {
		this.rejected += count;
	}

	summary() {
		return {
			modelCalls: this.models,
			toolCalls: this.tools,
			rejectedTools: this.rejected,
			estimatedTokens: this.estimatedTokens,
			actualTokens: this.actualTokens,
			accountedTokens: this.accountedTokens,
		};
	}
}
