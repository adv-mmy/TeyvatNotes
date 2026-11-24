import { createInterface } from "node:readline";
import {
	SessionError,
	type SessionHistory,
	type SessionStore,
	type TurnOutcome,
	type WritableSession,
} from "../application/ports/session.js";
import type { RunTurn } from "../application/run-turn.js";

const messages: Readonly<Record<string, string>> = {
	SESSION_ID_INVALID: "会话 ID 无效。",
	SESSION_NOT_FOUND: "未找到会话。",
	SESSION_BUSY:
		"会话已被另一个进程打开。只能查看已提交历史，请在对方退出后重新打开。",
	SESSION_CORRUPT: "会话记录损坏。只显示可恢复的历史，不覆盖原记录。",
	SESSION_FAILED: "未能读取或持久化会话。请重新打开后核对历史。",
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
};

function safe(text: string) {
	return Array.from(text, (char) => {
		const code = char.codePointAt(0) ?? 0;
		return code === 10 || (code >= 32 && !(code >= 127 && code <= 159))
			? char
			: " ";
	}).join("");
}

export async function runCli(
	store: SessionStore,
	turn: RunTurn,
	input: NodeJS.ReadableStream = process.stdin,
	output: NodeJS.WritableStream = process.stdout,
): Promise<void> {
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
	let conditionDraft: string | undefined;
	let closing = false;

	function show(outcome: TurnOutcome, allowReply = false) {
		switch (outcome.status) {
			case "answered":
				print(outcome.text);
				for (const cite of outcome.citations)
					print(
						`引用：${cite.source}｜${cite.version}｜${cite.location}\n${cite.excerpt}`,
					);
				break;
			case "needs_clarification":
				print(`需要补充条件：${outcome.text}`);
				if (allowReply) print("请直接输入要补充的条件。");
				break;
			case "insufficient_evidence":
				print(`资料不足：${outcome.text}`);
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
		conditionDraft = undefined;
	}

	function showPendingClarification(session: WritableSession) {
		const outcome = session.history().turns.at(-1)?.outcome;
		if (outcome?.status === "needs_clarification") show(outcome, true);
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
				showPendingClarification(state.session);
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
		} else {
			let prompt = line;
			let confirmedConditions: { clarification: string } | undefined;
			if (
				selected.session.history().turns.at(-1)?.outcome.status ===
				"needs_clarification"
			) {
				if (line === "取消") {
					conditionDraft = undefined;
					print("已放弃待确认的条件。可重新输入。");
					return;
				}
				if (line !== "确认") {
					conditionDraft = line;
					print(
						"补充条件待确认。输入「确认」保存并继续，输入「取消」放弃，或重新输入替换。",
					);
					return;
				}
				if (!conditionDraft) {
					print("没有待确认的条件。请先输入要补充的条件。");
					return;
				}
				prompt = conditionDraft;
				confirmedConditions = { clarification: conditionDraft };
				conditionDraft = undefined;
			}
			cancellation = new AbortController();
			try {
				const result = await turn.runTurn(
					selected.session,
					{
						prompt,
						...(confirmedConditions ? { confirmedConditions } : {}),
						onProgress: (stage) =>
							print(
								stage === "model" ? "正在调用受控模型…" : "正在执行受控工具…",
							),
					},
					cancellation.signal,
				);
				show(result.outcome, true);
			} finally {
				cancellation = undefined;
			}
		}
	}

	function interrupt() {
		if (cancellation) cancellation.abort();
		else if (conditionDraft) {
			conditionDraft = undefined;
			print("已放弃待确认的条件。可重新输入。");
		} else if (!running) {
			closing = true;
			rl.close();
		}
	}

	process.on("SIGINT", interrupt);
	rl.on("SIGINT", interrupt);
	print("TeyvatNotes V1：离线受控模型与工具，不提供真实攻略。");
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
