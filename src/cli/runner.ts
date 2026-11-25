import { createInterface } from "node:readline";
import type { IngestErrorCode } from "../application/ingest-guide.js";
import {
	SessionError,
	type SessionHistory,
	type SessionStore,
	type TurnOutcome,
	type WritableSession,
} from "../application/ports/session.js";
import type { RunTurn } from "../application/run-turn.js";

const messages: Readonly<Record<string, string>> = {
	WEB_SOURCE_UNAVAILABLE: "联网资料源不可用，本轮未重试。",
	INDEX_NOT_READY: "补查资料的索引未就绪，未发布为可引用攻略。",
	SESSION_ID_INVALID: "会话 ID 无效。",
	SESSION_NOT_FOUND: "未找到会话。",
	SESSION_BUSY:
		"会话已被另一个进程打开。只能查看已提交历史，请在对方退出后重新打开。",
	SESSION_CORRUPT: "会话记录损坏。只显示可恢复的历史，不覆盖原记录。",
	SESSION_FAILED: "未能读取或持久化会话。请重新打开后核对历史。",
	MODEL_NOT_CONFIGURED: "当前未配置正式问答模型，仍可查看本地会话历史。",
	MODEL_CONFIGURATION_INVALID:
		"回答模型配置无效，请检查 HTTPS 地址、模型 ID 和密钥文件。仍可查看历史。",
	GUIDE_STORAGE_UNAVAILABLE:
		"攻略存储配置不可用，请检查数据库密钥及服务配置。仍可查看历史。",
	TERMINOLOGY_UNAVAILABLE: "未能读取本地术语表，仍可查看本地会话历史。",
	TERMINOLOGY_INVALID: "本地术语表格式无效，仍可查看本地会话历史。",
	BUDGET_EXHAUSTED: "本轮达到调用次数或累计 Token 上限。",
	CONTEXT_WINDOW_EXCEEDED: "本次请求与预留输出超出上下文窗口。",
	MODEL_TIMEOUT: "模型调用超时。",
	TOOL_TIMEOUT: "工具调用超时。",
	TURN_TIMEOUT: "本轮超过总时限。",
	MODEL_FAILED: "模型调用失败。",
	TOOL_FAILED: "工具执行失败。",
	MODEL_OUTPUT_LIMIT: "回答达到输出上限，未生成完整答复。",
	INVALID_ANSWER_STRUCTURE: "回答不符合格式，未展示。",
	INVALID_CITATION: "回答引用了未经本轮回读的证据，未展示。",
	INVALID_ANSWER_CONDITIONS: "回答与已确认条件或证据前提不符，未展示。",
	RETRIEVAL_INCOMPLETE: "攻略检索尚未查完，请调整查询或稍后再试。",
	RETRIEVAL_UNAVAILABLE: "攻略检索或正文读取暂不可用。",
};

function safe(text: string) {
	return Array.from(text, (char) => {
		const code = char.codePointAt(0) ?? 0;
		return code === 10 || (code >= 32 && !(code >= 127 && code <= 159))
			? char
			: " ";
	}).join("");
}

export type MaintenanceCommand =
	| { readonly kind: "migrate" }
	| {
			readonly kind: "import";
			readonly file: string;
			readonly publish: boolean;
	  }
	| { readonly kind: "publish"; readonly id: string };

export function parseMaintenance(
	args: readonly string[],
): MaintenanceCommand | undefined {
	if (args.length === 2 && args[0] === "db" && args[1] === "migrate")
		return { kind: "migrate" };
	if (
		args[0] === "guides" &&
		args[1] === "import" &&
		args[2] &&
		(args.length === 3 || (args.length === 4 && args[3] === "--publish"))
	)
		return { kind: "import", file: args[2], publish: args[3] === "--publish" };
	if (
		args[0] === "guides" &&
		args[1] === "publish" &&
		args.length === 3 &&
		args[2] &&
		/^[a-f0-9]{64}$/u.test(args[2])
	)
		return { kind: "publish", id: args[2] };
}

export function maintenanceMessage(
	code:
		| IngestErrorCode
		| "MIGRATION_CHECKSUM_MISMATCH"
		| "MAINTENANCE_TIMEOUT"
		| "MAINTENANCE_USAGE",
) {
	const errors = {
		GUIDE_INVALID: "攻略文件无法读取、格式无效或缺少可分块正文。",
		GUIDE_METADATA_INCOMPLETE:
			"请明确提供攻略类型、来源、标准角色、适用版本和适用条件。",
		GUIDE_NOT_FOUND: "未找到此文档版本或当前 Embedding 配置下的索引记录。",
		INDEX_NOT_READY:
			"攻略索引未就绪，未执行发布。请检查 Ollama 模型和 ChromaDB，再显式导入。",
		GUIDE_STORAGE_UNAVAILABLE:
			"无法访问或初始化 PostgreSQL 攻略库。请检查配置、依赖和迁移状态。",
		MIGRATION_CHECKSUM_MISMATCH: "已执行迁移的校验和不匹配，未改动数据库。",
		MAINTENANCE_TIMEOUT: "维护命令已超时或中断，请检查结果后再显式执行。",
		MAINTENANCE_USAGE:
			"用法：teyvatnotes db migrate | guides import <文件.md 或 .json> [--publish] | guides publish <文档版本 ID>",
	};
	return `${code}：${errors[code]}`;
}

export type QuestionAvailability =
	| { readonly kind: "ready"; readonly turn: RunTurn }
	| {
			readonly kind: "unavailable";
			readonly code:
				| "MODEL_NOT_CONFIGURED"
				| "MODEL_CONFIGURATION_INVALID"
				| "GUIDE_STORAGE_UNAVAILABLE"
				| "TERMINOLOGY_INVALID"
				| "TERMINOLOGY_UNAVAILABLE";
	  };

export async function runCli(
	store: SessionStore,
	questions: QuestionAvailability,
	input: NodeJS.ReadableStream = process.stdin,
	output: NodeJS.WritableStream = process.stdout,
): Promise<void> {
	const turn = questions.kind === "ready" ? questions.turn : undefined;
	const print = (line: string) => {
		output.write(`${safe(line)}\n`);
	};
	const error = (code: string) =>
		print(`${code}：${messages[code] ?? "本轮未能完成。"}`);
	const rl = createInterface({
		input,
		output,
		terminal: Boolean("isTTY" in input && input.isTTY),
	});
	let selected:
		| { session: WritableSession }
		| {
				id: string;
				code: "SESSION_BUSY" | "SESSION_CORRUPT";
				history?: SessionHistory;
		  }
		| undefined;
	let running: Promise<void> | undefined;
	let cancellation: AbortController | undefined;
	let closing = false;

	function show(outcome: TurnOutcome, allowReply = false) {
		switch (outcome.status) {
			case "answered":
				print(outcome.text);
				for (const cite of outcome.citations)
					print(
						`引用：${cite.source}｜${cite.version}｜${cite.location}\n${cite.excerpt}${cite.url ? `\n原文：${cite.url}` : ""}`,
					);
				break;
			case "needs_clarification":
				print(`需要补充条件：${outcome.text}`);
				if (allowReply) print("请直接输入要补充的条件。");
				break;
			case "insufficient_evidence":
				print(
					`资料不足：${
						outcome.supplementGap
							? {
									no_results: "联网补查未找到相关攻略。",
									source_restricted: "没有获准且可读取正文的攻略来源。",
									no_readable_body: "找到了攻略链接，但没有可读取的正文。",
									metadata_incomplete:
										"攻略缺少明确的适用信息或存在歧义，未发布。",
									scope_mismatch: "找到的攻略不适用于本轮角色或游戏版本。",
								}[outcome.supplementGap]
							: outcome.text
					}`,
				);
				break;
			case "failed":
				error(outcome.code);
				break;
			case "cancelled":
				print("本轮已取消。");
				break;
			case "interrupted":
				print("本轮已中断，未重放。");
				break;
		}
	}

	async function closeSession() {
		if (selected && "session" in selected) await selected.session.close();
		selected = undefined;
	}

	async function handle(line: string) {
		if (line === "/new") {
			await closeSession();
			const session = await store.create();
			selected = { session };
			print(`已创建会话 ${session.sessionId}`);
		} else if (line.startsWith("/open ")) {
			await closeSession();
			const state = await store.open(line.slice(6).trim());
			if (state.mode === "write") {
				selected = { session: state.session };
				print(`已打开会话 ${state.session.sessionId}`);
				const pending = turn?.pendingClarification(state.session);
				if (pending) print(pending);
			} else {
				selected = {
					id: state.sessionId,
					code: state.code,
					...(state.history ? { history: state.history } : {}),
				};
				error(state.code);
			}
		} else if (line === "/history") {
			if (!selected) {
				print("请先使用 /new 或 /open <会话 ID>。");
				return;
			}
			const history =
				"session" in selected
					? selected.session.history()
					: (selected.history ?? (await store.readHistory(selected.id)));
			if (!history.turns.length) print("尚无已提交历史。");
			for (const item of history.turns) {
				print(`问题：${item.prompt}`);
				show(item.outcome);
			}
		} else if (line === "/exit") {
			closing = true;
			rl.close();
		} else if (line.startsWith("/")) {
			print("命令：/new、/open <会话 ID>、/history、/exit。");
		} else if (!selected) {
			print("请先使用 /new 或 /open <会话 ID>。");
		} else if (!("session" in selected)) {
			error(selected.code);
		} else if (questions.kind === "unavailable") {
			error(questions.code);
		} else {
			cancellation = new AbortController();
			try {
				const result = await questions.turn.submit(
					selected.session,
					line,
					cancellation.signal,
					(stage) =>
						print(stage === "model" ? "正在调用模型…" : "正在执行工具…"),
				);
				if (result.kind === "notice") print(result.text);
				else show(result.turn.outcome, true);
			} finally {
				cancellation = undefined;
			}
		}
	}

	function interrupt() {
		if (cancellation) {
			cancellation.abort();
			return;
		}
		const discarded =
			selected && "session" in selected
				? turn?.discardDraft(selected.session)
				: undefined;
		if (discarded) print(discarded);
		else if (!running) {
			closing = true;
			rl.close();
		}
	}

	process.on("SIGINT", interrupt);
	rl.on("SIGINT", interrupt);
	print(
		turn ? "TeyvatNotes" : "TeyvatNotes：问答未配置，可使用本地会话和历史。",
	);
	if (
		questions.kind === "unavailable" &&
		questions.code !== "MODEL_NOT_CONFIGURED"
	)
		error(questions.code);
	print("命令：/new、/open <会话 ID>、/history、/exit。");
	try {
		await new Promise<void>((resolve) => {
			rl.on("line", (value) => {
				const line = value.trim();
				if (!line || closing) return;
				if (running) {
					print("当前操作尚未结束，输入未排队。");
					return;
				}
				running = handle(line)
					.catch((cause: unknown) => {
						error(
							cause instanceof SessionError ? cause.code : "SESSION_FAILED",
						);
					})
					.finally(() => {
						running = undefined;
					});
			});
			rl.once("close", resolve);
		});
		await running;
	} finally {
		process.off("SIGINT", interrupt);
		await closeSession();
	}
}
