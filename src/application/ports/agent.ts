export type JsonValue =
	| null
	| boolean
	| number
	| string
	| readonly JsonValue[]
	| { readonly [key: string]: JsonValue };
export type JsonObject = { readonly [key: string]: JsonValue };

export interface ToolDefinition {
	readonly name: string;
	readonly description: string;
	readonly parameters: JsonObject;
}

export interface ToolCall {
	readonly id: string;
	readonly name: string;
	readonly arguments: JsonObject;
}

export type ToolRejection = "TOOL_NOT_ALLOWED" | "INVALID_TOOL_ARGUMENT";

/** 同一条模型回复形成一批。rejected 仅供记账，不能执行。 */
export interface ToolBatch {
	readonly calls: readonly ToolCall[];
	readonly rejected: readonly {
		id: string;
		name: string;
		code: ToolRejection;
	}[];
	readonly signal: AbortSignal;
}

export type ToolResult =
	| { readonly id: string; readonly ok: true; readonly content: JsonValue }
	| {
			readonly id: string;
			readonly ok: false;
			readonly code: ToolRejection | "TOOL_FAILED" | "TOOL_TIMEOUT";
	  };

export interface AgentRunInput {
	readonly prompt: string;
	readonly tools: readonly ToolDefinition[];
	readonly turnId?: string;
	readonly priorTurns?: readonly {
		readonly prompt: string;
		readonly answer: string;
	}[];
	readonly conditions?: JsonObject;
}

export interface ModelRequestDraft {
	/** 本次发送候选的完整序列化会话上下文；仅供应用检查，不记日志。 */
	readonly serializedContext: string;
	readonly signal: AbortSignal;
}

export type ModelGateError = "BUDGET_EXHAUSTED" | "CONTEXT_WINDOW_EXCEEDED";
export type PreparedModelRequest =
	| {
			readonly allowed: true;
			readonly deadline?: AbortSignal;
			readonly maxOutputTokens?: number;
	  }
	| { readonly allowed: false; readonly code: ModelGateError };

export type AgentRunError =
	| ModelGateError
	| "INVALID_INPUT"
	| "INVALID_TOOL_DEFINITION"
	| "DUPLICATE_TOOL_CALL"
	| "INVALID_MODEL_RESPONSE"
	| "INVALID_TOOL_RESULT"
	| "MODEL_FAILED"
	| "MODEL_TIMEOUT"
	| "MODEL_OUTPUT_LIMIT"
	| "TOOL_BATCH_FAILED"
	| "RUNTIME_FAILED";

export type AgentRunResult =
	| { readonly status: "completed"; readonly text: string }
	| { readonly status: "cancelled" }
	| { readonly status: "failed"; readonly code: AgentRunError };

export interface AgentRunHost {
	prepareModelRequest(draft: ModelRequestDraft): Promise<PreparedModelRequest>;
	executeToolBatch(batch: ToolBatch): Promise<readonly ToolResult[]>;
	recordModelUsage?(tokens: number): void;
}

/** 同时中止实际操作和对它的等待，避免非正常传输阻塞 Pi 的停止。 */
export function untilAborted<T>(
	work: () => T | Promise<T>,
	signal: AbortSignal,
): Promise<T> {
	return new Promise((resolve, reject) => {
		if (signal.aborted) {
			reject(signal.reason);
			return;
		}
		const abort = () => reject(signal.reason);
		signal.addEventListener("abort", abort, { once: true });
		void Promise.resolve()
			.then(() => {
				signal.throwIfAborted();
				return work();
			})
			.then(resolve, reject)
			.finally(() => signal.removeEventListener("abort", abort));
	});
}

export interface AgentRuntime {
	run(
		input: AgentRunInput,
		host: AgentRunHost,
		signal: AbortSignal,
	): Promise<AgentRunResult>;
}
