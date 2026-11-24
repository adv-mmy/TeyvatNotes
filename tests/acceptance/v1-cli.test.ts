import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import {
	appendFile,
	mkdtemp,
	readdir,
	readFile,
	realpath,
	rename,
	rm,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { type TestContext, test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { PiSessionStore } from "../../src/adapters/pi/session-store.js";
import { readConfig } from "../../src/bootstrap/config.js";

const linuxOnly = { skip: process.platform !== "linux", timeout: 30_000 };
const entry = fileURLToPath(
	new URL("../../src/bootstrap/main.js", import.meta.url),
);
const fixture = fileURLToPath(
	new URL("../fixtures/pi-controlled-model.js", import.meta.url),
);

async function rootFor(t: TestContext) {
	const base = await realpath(tmpdir());
	const root = await mkdtemp(join(base, "tn-v1-cli-"));
	t.after(async () => {
		const absolute = await realpath(root);
		assert.equal(dirname(absolute), base);
		assert.match(basename(absolute), /^tn-v1-cli-[A-Za-z0-9]+$/);
		await rm(absolute, { recursive: true });
	});
	return root;
}

function start(
	t: TestContext,
	directory: string,
	scenario?: string,
	env: NodeJS.ProcessEnv = {},
) {
	const child = spawn(
		process.execPath,
		scenario ? [fixture, scenario] : [entry],
		{
			env: {
				...process.env,
				TEYVAT_DIAGNOSTICS: "1",
				TEYVAT_SESSION_DIR: directory,
				...env,
			},
		},
	);
	const exited = once(child, "close");
	t.after(async () => {
		if (child.exitCode === null && child.signalCode === null)
			child.kill("SIGKILL");
		await exited;
	});
	let stdout = "";
	let stderr = "";
	let cursor = 0;
	child.stdout.setEncoding("utf8").on("data", (line: string) => {
		stdout += line;
	});
	child.stderr.setEncoding("utf8").on("data", (line: string) => {
		stderr += line;
	});
	child.stdin.on("error", () => {});

	return {
		child,
		send(line: string) {
			child.stdin.write(`${line}\n`);
		},
		output: () => stdout,
		logs: () => stderr,
		async wait(pattern: RegExp, source: "stdout" | "stderr" = "stdout") {
			for (let index = 0; index < 700; index++) {
				const match = (
					source === "stdout" ? stdout.slice(cursor) : stderr
				).match(pattern);
				if (match) {
					if (source === "stdout")
						cursor += (match.index ?? 0) + match[0].length;
					return match;
				}
				if (child.exitCode !== null) break;
				await delay(15);
			}
			assert.fail(`未收到 ${pattern}；stdout=${stdout} stderr=${stderr}`);
		},
		async exit() {
			child.stdin.end("/exit\n");
			assert.deepEqual(await exited, [0, null], stderr);
		},
		async kill(signal: NodeJS.Signals) {
			child.kill(signal);
			await exited;
		},
	};
}

async function create(client: ReturnType<typeof start>) {
	client.send("/new");
	const result = await client.wait(/已创建会话 ([0-9a-f-]{36})/);
	assert.ok(result[1]);
	return result[1];
}

async function journal(root: string, id: string) {
	return (await readFile(join(root, id, "turns.jsonl"), "utf8"))
		.trimEnd()
		.split("\n")
		.filter(Boolean)
		.map((line) => JSON.parse(line));
}

function diagnostics(client: ReturnType<typeof start>) {
	return client
		.logs()
		.split("\n")
		.filter((line) => line.startsWith("{"))
		.map((line) => JSON.parse(line));
}

test(
	"真实 CLI 离线运行两个分支，先提交后展示；重开只看已提交历史",
	linuxOnly,
	async (t) => {
		const root = await rootFor(t);
		const client = start(t, root);
		const id = await create(client);
		client.send("   ");
		client.send("/history");
		await client.wait(/尚无已提交历史/);
		assert.equal((await journal(root, id)).length, 0);
		client.send("请演示回读");
		await client.wait(/引用：TeyvatNotes 隔离测试样本/);
		const rows = await journal(root, id);
		assert.equal(rows[1].outcome.status, "answered");
		assert.equal(rows[1].outcome.citations[0].location, "fixture:sample/1");
		const piRows = (await readFile(join(root, id, rows[1].pi.file), "utf8"))
			.trimEnd()
			.split("\n")
			.map((line) => JSON.parse(line));
		assert.ok(rows[1].pi.entryIds.length >= 6);
		for (const name of ["probe_1", "read_2"])
			assert.ok(
				piRows.some(
					({ message }) =>
						message?.role === "toolResult" && message.toolCallId === name,
				),
			);
		client.send("请演示缺口");
		await client.wait(/资料不足：受控资料集中没有适用的样本/);
		await client.exit();
		const usage = diagnostics(client);
		assert.deepEqual(
			usage.map(({ modelCalls, toolCalls }) => [modelCalls, toolCalls]),
			[
				[3, 2],
				[3, 2],
			],
		);
		assert.doesNotMatch(
			client.logs(),
			/请演示|模拟资料|teyvatnotes-controlled-only/,
		);
		const opened = start(t, root);
		opened.send(`/open ${id.toUpperCase()}`);
		await opened.wait(/已打开会话/);
		opened.send("/history");
		await opened.wait(/资料不足：受控资料集中没有适用的样本/);
		assert.match(opened.output(), /已回读模拟资料/);
		await opened.exit();
		assert.equal(diagnostics(opened).length, 0);
		assert.equal((await journal(root, id)).length, 4);
	},
);

test(
	"同会话双进程互斥；SIGKILL 后标记中断，不展示或重放 Pi 草稿",
	linuxOnly,
	async (t) => {
		const root = await rootFor(t);
		const first = start(t, root);
		const id = await create(first);
		first.send("已提交问题");
		await first.wait(/引用：/);
		await first.exit();

		const writer = start(t, root, "hold");
		writer.send(`/open ${id}`);
		await writer.wait(/已打开会话/);
		writer.send("待中断问题");
		await writer.wait(/MODEL_WAITING/, "stderr");
		const contender = start(t, root);
		contender.send(`/open ${id}`);
		await contender.wait(/SESSION_BUSY/);
		contender.send("/history");
		await contender.wait(/问题：已提交问题/);
		assert.doesNotMatch(contender.output(), /待中断问题|内部草稿|已中断/);
		contender.send("不允许追加");
		await contender.wait(/SESSION_BUSY/);
		assert.equal((await journal(root, id)).length, 3);
		await writer.kill("SIGKILL");

		contender.send(`/open ${id}`);
		await contender.wait(/已打开会话/);
		contender.send("/history");
		await contender.wait(/本轮已中断，未重放/);
		assert.doesNotMatch(contender.output(), /内部草稿|不允许追加.*问题/);
		const recovered = await journal(root, id);
		assert.equal(recovered[3].outcome.status, "interrupted");
		assert.ok(recovered[3].pi.entryIds.length > 0);
		contender.send(`/open ${id}`);
		await contender.wait(/已打开会话/);
		await contender.exit();
		assert.equal(diagnostics(contender).length, 0);
		assert.deepEqual(await journal(root, id), recovered);
		const files = await readdir(join(root, id));
		assert.equal(files.filter((name) => name.endsWith(".jsonl")).length, 3);
	},
);

test(
	"真实 CLI 追问补充：草稿不落盘，已确认条件在轮次中断后仍供下一轮使用",
	linuxOnly,
	async (t) => {
		const root = await rootFor(t);
		const first = start(t, root, "clarification");
		const id = await create(first);
		first.send("请演示追问");
		await first.wait(/需要补充条件：请补充希望排除的演示样本/);
		await first.wait(/请直接输入要补充的条件/);
		const original = await journal(root, id);
		assert.equal(original[1].outcome.status, "needs_clarification");
		first.send("未确认的样本 X");
		await first.wait(/补充条件待确认/);
		assert.deepEqual(await journal(root, id), original);
		assert.equal(diagnostics(first).length, 1);
		await first.kill("SIGKILL");

		const writer = start(t, root, "clarification-hold");
		writer.send(`/open ${id}`);
		await writer.wait(/需要补充条件：请补充希望排除的演示样本/);
		writer.send("确认");
		await writer.wait(/没有待确认的条件/);

		const reader = start(t, root);
		reader.send(`/open ${id}`);
		await reader.wait(/SESSION_BUSY/);
		reader.send("/history");
		await reader.wait(/需要补充条件：请补充希望排除的演示样本/);
		reader.send("越权补充");
		await reader.wait(/SESSION_BUSY/);
		reader.send("确认");
		await reader.wait(/SESSION_BUSY/);
		await reader.exit();
		assert.equal(diagnostics(reader).length, 0);

		writer.send("取消的样本 A");
		await writer.wait(/补充条件待确认/);
		writer.send("取消");
		await writer.wait(/已放弃待确认的条件/);
		assert.deepEqual(await journal(root, id), original);
		writer.send("排除样本 B");
		await writer.wait(/补充条件待确认/);
		writer.send("确认");
		await writer.wait(/CONFIRMED_MODEL_WAITING/, "stderr");
		const active = await journal(root, id);
		assert.equal(active[2].type, "turn_started");
		assert.equal(active[3].type, "conditions_confirmed");
		assert.equal(active[3].turnId, active[2].turnId);
		assert.deepEqual(active[3].conditions, { clarification: "排除样本 B" });
		assert.equal(active.length, 4);
		await writer.kill("SIGKILL");

		const reopened = start(t, root, "require-confirmed");
		reopened.send(`/open ${id}`);
		await reopened.wait(/已打开会话/);
		reopened.send("/history");
		await reopened.wait(/本轮已中断，未重放/);
		assert.equal(diagnostics(reopened).length, 0);
		assert.equal((await journal(root, id))[4].outcome.status, "interrupted");
		assert.doesNotMatch(
			reopened.output(),
			/未确认的样本 X|取消的样本 A|越权补充/,
		);
		reopened.send("继续受控演示");
		await reopened.wait(/引用：TeyvatNotes 隔离测试样本/);
		await reopened.exit();
		assert.deepEqual(
			diagnostics(reopened).map(({ modelCalls, toolCalls }) => [
				modelCalls,
				toolCalls,
			]),
			[[3, 2]],
		);
		assert.doesNotMatch(reopened.logs(), /排除样本 B|继续受控演示/);
	},
);

test(
	"受控追问在同一 CLI 确认后继续，并提交带引用的回答",
	linuxOnly,
	async (t) => {
		const root = await rootFor(t);
		const client = start(t, root, "clarification");
		const id = await create(client);
		client.send("请演示追问");
		await client.wait(/需要补充条件/);
		client.send("排除样本 B");
		await client.wait(/补充条件待确认/);
		client.send("确认");
		await client.wait(/引用：TeyvatNotes 隔离测试样本/);
		await client.exit();
		const rows = await journal(root, id);
		assert.deepEqual(rows[3].conditions, { clarification: "排除样本 B" });
		assert.equal(rows[4].outcome.status, "answered");
		assert.deepEqual(
			diagnostics(client).map(({ modelCalls, toolCalls }) => [
				modelCalls,
				toolCalls,
			]),
			[
				[1, 0],
				[3, 2],
			],
		);
	},
);

test("带引用字段的非法追问不会展示或进入条件确认", linuxOnly, async (t) => {
	const root = await rootFor(t);
	const client = start(t, root, "invalid-clarification");
	const id = await create(client);
	client.send("验证非法追问");
	await client.wait(/INVALID_ANSWER_STRUCTURE/);
	await client.exit();
	assert.doesNotMatch(client.output(), /需要补充条件|请补充希望排除/);
	assert.deepEqual((await journal(root, id))[1].outcome, {
		status: "failed",
		code: "INVALID_ANSWER_STRUCTURE",
	});
});

test(
	"实际提交写入失败：CLI 不展示成功正文，不重发模型或工具",
	linuxOnly,
	async (t) => {
		const root = await rootFor(t);
		const client = start(t, root, "commit-failure");
		const id = await create(client);
		const path = join(root, id, "turns.jsonl");
		const held = join(root, id, "turns.test-held");
		client.send("验证提交失败");
		await client.wait(/COMMIT_WAITING/, "stderr");
		assert.deepEqual(
			(await journal(root, id)).map(({ type }) => type),
			["turn_started"],
		);
		// 只作用于本用例的新建会话：让真实提交的追加打开失败，而不是伪造业务错误。
		await rename(path, held);
		try {
			assert.ok(client.child.kill("SIGUSR2"));
			await client.wait(/SESSION_FAILED/);
			client.send("/history");
			await client.wait(/尚无已提交历史/);
			await client.exit();
		} finally {
			await rename(held, path);
		}
		assert.doesNotMatch(client.output(), /已回读模拟资料|引用：/);
		assert.deepEqual(
			diagnostics(client).map(({ modelCalls, toolCalls, errorCode }) => [
				modelCalls,
				toolCalls,
				errorCode,
			]),
			[[3, 2, "SESSION_FAILED"]],
		);
		assert.deepEqual(
			(await journal(root, id)).map(({ type }) => type),
			["turn_started"],
		);

		const reopened = start(t, root);
		reopened.send(`/open ${id}`);
		await reopened.wait(/已打开会话/);
		reopened.send("/history");
		await reopened.wait(/本轮已中断，未重放/);
		await reopened.exit();
		assert.doesNotMatch(reopened.output(), /已回读模拟资料|引用：/);
		assert.equal(diagnostics(reopened).length, 0);
		assert.equal((await journal(root, id))[1].outcome.status, "interrupted");
	},
);

test(
	"提交日志末尾不完整时只读恢复，不覆盖原文件；显式条件仍保留",
	linuxOnly,
	async (t) => {
		const root = await rootFor(t);
		const store = new PiSessionStore(root, process.cwd());
		const session = await store.create();
		const id = session.sessionId;
		const turnId = await session.begin("条件写入后中断");
		await session.appendConfirmedConditions({
			turnId,
			conditions: { samples: [] },
		});
		await session.close();
		const file = join(root, id, "turns.jsonl");
		await appendFile(
			file,
			'{"type":"turn_committed","outcome":{"status":"answered","text":"未提交正文',
		);
		const before = await readFile(file);
		const state = await store.open(id);
		assert.equal(state.mode, "read");
		assert.ok(state.mode === "read");
		assert.equal(state.code, "SESSION_CORRUPT");
		assert.deepEqual(state.history?.conditions, { samples: [] });
		assert.equal(state.history?.turns[0]?.outcome.status, "interrupted");
		assert.equal((await store.readHistory(id)).turns.length, 0);

		const client = start(t, root);
		client.send(`/open ${id}`);
		await client.wait(/SESSION_CORRUPT/);
		client.send("/history");
		await client.wait(/本轮已中断/);
		assert.doesNotMatch(client.output(), /未提交正文/);
		client.send("不能追加");
		await client.wait(/SESSION_CORRUPT/);
		await client.exit();
		assert.deepEqual(await readFile(file), before);
	},
);

test(
	"拒绝未知工具、非法参数和同批未授权回读；伪造引用不展示",
	linuxOnly,
	async (t) => {
		const root = await rootFor(t);
		const illegal = start(t, root, "illegal", {
			TEYVAT_MAX_OUTPUT_TOKENS: "4096",
		});
		await create(illegal);
		illegal.send("验证权限");
		await illegal.wait(/引用：/);
		await illegal.exit();
		assert.equal(diagnostics(illegal)[0].rejectedTools, 3);
		assert.equal(diagnostics(illegal)[0].toolCalls, 2);
		const fake = start(t, root, "fake-citation");
		const id = await create(fake);
		fake.send("检查引用");
		await fake.wait(/INVALID_CITATION/);
		await fake.exit();
		assert.doesNotMatch(fake.output(), /禁止展示的伪造答复/);
		assert.equal((await journal(root, id))[1].outcome.code, "INVALID_CITATION");
	},
);

for (const [scenario, env, expected, models, tools] of [
	["normal", { TEYVAT_MODEL_CALLS: "1" }, "BUDGET_EXHAUSTED", 1, 1],
	["normal", { TEYVAT_TOOL_CALLS: "1" }, "BUDGET_EXHAUSTED", 2, 1],
	["normal", { TEYVAT_CONTEXT_TOKENS: "100" }, "CONTEXT_WINDOW_EXCEEDED", 0, 0],
	["normal", { TEYVAT_CUMULATIVE_TOKENS: "100" }, "BUDGET_EXHAUSTED", 0, 0],
	["model-error", {}, "MODEL_FAILED", 1, 0],
	["tool-error", {}, "TOOL_FAILED", 1, 1],
	["model-timeout", { TEYVAT_MODEL_MS: "200" }, "MODEL_TIMEOUT", 1, 0],
	["tool-timeout", { TEYVAT_TOOL_MS: "200" }, "TOOL_TIMEOUT", 1, 1],
	["model-timeout", { TEYVAT_TURN_MS: "700" }, "TURN_TIMEOUT", 1, 0],
] as const) {
	test(
		`CLI 到达 ${expected} 后停止：${scenario} ${JSON.stringify(env)}`,
		linuxOnly,
		async (t) => {
			const root = await rootFor(t);
			const client = start(t, root, scenario, env);
			const id = await create(client);
			client.send("验证预算和停止");
			await client.wait(new RegExp(expected));
			await client.exit();
			assert.doesNotMatch(client.logs(), /未公开|验证预算和停止/);
			const row = diagnostics(client)[0];
			assert.deepEqual([row.modelCalls, row.toolCalls], [models, tools]);
			assert.deepEqual((await journal(root, id))[1].outcome, {
				status: "failed",
				code: expected,
			});
		},
	);
}

test("Ctrl+C 取消进行中的轮次；运行中的新问题不排队", linuxOnly, async (t) => {
	const root = await rootFor(t);
	const client = start(t, root, "hold");
	const id = await create(client);
	client.send("进行中的受控问题");
	await client.wait(/MODEL_WAITING/, "stderr");
	client.send("绝不排队");
	await client.wait(/输入未排队/);
	client.child.kill("SIGINT");
	await client.wait(/本轮已取消/);
	client.send("/history");
	await client.wait(/本轮已取消/);
	await client.exit();
	const rows = await journal(root, id);
	assert.equal(rows.length, 2);
	assert.equal(rows[1].outcome.status, "cancelled");
	assert.deepEqual(
		diagnostics(client).map(({ modelCalls, toolCalls }) => [
			modelCalls,
			toolCalls,
		]),
		[[2, 1]],
	);
	assert.doesNotMatch(client.output(), /内部草稿|绝不排队/);
});

test(
	"已落盘但快照未发布的提交可重建，不生成第二份提交",
	linuxOnly,
	async (t) => {
		const root = await rootFor(t);
		const store = new PiSessionStore(root, process.cwd());
		const session = await store.create();
		const turnId = await session.begin("持久化后、展示前退出");
		await session.close();
		const file = join(root, session.sessionId, "turns.jsonl");
		const outcome = {
			status: "answered",
			text: "已提交的固定答复",
			citations: [
				{
					source: "隔离测试",
					version: "V1",
					location: "fixture:1",
					excerpt: "证据摘录",
				},
			],
		};
		const event = {
			type: "turn_committed",
			sessionId: session.sessionId,
			at: new Date().toISOString(),
			turnId,
			outcome,
			pi: null,
		};
		await appendFile(file, `${JSON.stringify(event)}\n`);
		assert.equal((await store.readHistory(session.sessionId)).turns.length, 0);
		const client = start(t, root);
		client.send(`/open ${session.sessionId}`);
		await client.wait(/已打开会话/);
		client.send("/history");
		await client.wait(/引用：隔离测试｜V1｜fixture:1/);
		await client.exit();
		assert.equal(diagnostics(client).length, 0);
		assert.equal((await journal(root, session.sessionId)).length, 2);
		assert.deepEqual(
			(await store.readHistory(session.sessionId)).turns[0]?.outcome,
			outcome,
		);
	},
);

test("输出限制默认 8192，可在受控模型支持范围内调整", () => {
	assert.equal(readConfig({}).limits.maxOutputTokens, 8192);
	assert.equal(
		readConfig({ TEYVAT_MAX_OUTPUT_TOKENS: "16384" }).limits.maxOutputTokens,
		16384,
	);
	assert.throws(
		() => readConfig({ TEYVAT_MAX_OUTPUT_TOKENS: "16385" }),
		/TEYVAT_MAX_OUTPUT_TOKENS/,
	);
	assert.throws(
		() => readConfig({ TEYVAT_MAX_OUTPUT_TOKENS: "0" }),
		/TEYVAT_MAX_OUTPUT_TOKENS/,
	);
});
