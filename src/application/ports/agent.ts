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
}

export interface ModelRequestDraft {
	/** 本次发送候选的完整序列化会话上下文；仅供应用检查，不记日志。 */
	readonly serializedContext: string;
	readonly signal: AbortSignal;
}

export type ModelGateError = "BUDGET_EXHAUSTED" | "CONTEXT_WINDOW_EXCEEDED";
export type PreparedModelRequest =
	| { readonly allowed: true }
	| { readonly allowed: false; readonly code: ModelGateError };

export type AgentRunError =
	| ModelGateError
	| "INVALID_INPUT"
	| "INVALID_TOOL_DEFINITION"
	| "DUPLICATE_TOOL_CALL"
	| "INVALID_MODEL_RESPONSE"
	| "INVALID_TOOL_RESULT"
	| "MODEL_FAILED"
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
}

export interface AgentRuntime {
	run(
		input: AgentRunInput,
		host: AgentRunHost,
		signal: AbortSignal,
	): Promise<AgentRunResult>;
}
