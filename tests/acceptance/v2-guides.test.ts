import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, realpath, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { type TestContext, test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const linux = { skip: process.platform !== "linux", timeout: 30_000 };
const fixture = fileURLToPath(
	new URL("../fixtures/pi-controlled-model.js", import.meta.url),
);

async function open(t: TestContext, scenario: string) {
	const parent = await realpath(tmpdir());
	const root = await mkdtemp(join(parent, "tn-v2-guides-"));
	const child = spawn(process.execPath, [fixture, scenario], {
		env: { ...process.env, TEYVAT_SESSION_DIR: root, TEYVAT_DIAGNOSTICS: "1" },
	});
	const closed = once(child, "close");
	child.stdin.on("error", () => {});
	t.after(async () => {
		if (child.exitCode === null && child.signalCode === null)
			child.kill("SIGKILL");
		await closed;
		const path = await realpath(root);
		assert.equal(dirname(path), parent);
		assert.match(basename(path), /^tn-v2-guides-[a-zA-Z0-9]+$/u);
		await rm(path, { recursive: true });
	});
	let stdout = "";
	let stderr = "";
	let cursor = 0;
	child.stdout.setEncoding("utf8").on("data", (data: string) => {
		stdout += data;
	});
	child.stderr.setEncoding("utf8").on("data", (data: string) => {
		stderr += data;
	});
	const api = {
		send(line: string) {
			child.stdin.write(`${line}\n`);
		},
		async until(pattern: RegExp) {
			for (let n = 0; n < 700; n++) {
				const match = stdout.slice(cursor).match(pattern);
				if (match) {
					cursor += (match.index ?? 0) + match[0].length;
					return match;
				}
				if (child.exitCode !== null) break;
				await delay(15);
			}
			assert.fail(`未收到 ${pattern}；stdout=${stdout}\nstderr=${stderr}`);
		},
		async close() {
			child.stdin.end("/exit\n");
			assert.deepEqual(await closed, [0, null], stderr);
		},
		diagnostics() {
			return stderr
				.split("\n")
				.filter((line) => line.startsWith("{"))
				.map((line) => JSON.parse(line));
		},
		output() {
			return stdout;
		},
		errors() {
			return stderr;
		},
	};
	api.send("/new");
	await api.until(/已创建会话/u);
	return api;
}

for (const scenario of ["guides-hit", "guides-exact"]) {
	test(
		`真实 CLI＋Pi：向量续取或权威清单补查命中后，下一批回读才可引用（${scenario}）`,
		linux,
		async (t) => {
			const cli = await open(t, scenario);
			cli.send("小甲 7.1 怎么配队");
			await cli.until(/引用：隔离攻略 G-01｜7.1｜第 2 段/u);
			await cli.until(/原文：https:\/\/example\.test\/guide/u);
			await cli.close();
			const [trace] = cli.diagnostics();
			assert.equal(trace.status, "answered");
			assert.equal(trace.modelCalls, 3);
			assert.equal(trace.toolCalls, 2);
			assert.equal(trace.rejectedTools, scenario === "guides-hit" ? 1 : 0);
		},
	);
}

for (const [scenario, expected, toolCalls, modelCalls] of [
	["guides-miss", "资料不足", 1, 2],
	["guides-incomplete", "RETRIEVAL_INCOMPLETE", 1, 1],
	["guides-fault", "RETRIEVAL_UNAVAILABLE", 1, 1],
	["guides-missing-vector", "RETRIEVAL_UNAVAILABLE", 1, 1],
	["guides-truncated", "资料不足", 2, 3],
	["guides-forged", "INVALID_CITATION", 1, 2],
] as const) {
	test(`真实 CLI＋Pi：${scenario} 不展示无效证据`, linux, async (t) => {
		const cli = await open(t, scenario);
		cli.send("小甲 7.1 怎么配队");
		await cli.until(new RegExp(expected, "u"));
		await cli.close();
		assert.doesNotMatch(
			cli.output(),
			/引用：|索引依赖报错原文不可公开|参考配队：|可用配队：/u,
		);
		const [trace] = cli.diagnostics();
		assert.deepEqual(
			[trace.toolCalls, trace.modelCalls],
			[toolCalls, modelCalls],
		);
	});
}

for (const [scenario, detail] of [
	["guides-format-fence", "json_code_fence"],
	["guides-format-missing", "missing_field:$.recommendations[0].team"],
	["guides-format-conflict", "status_conflict:both_nonempty"],
] as const) {
	test(
		`真实 CLI＋Pi：攻略格式细分原因仅进入诊断（${detail}）`,
		linux,
		async (t) => {
			const cli = await open(t, scenario);
			cli.send("小甲 7.1 怎么配队；不能进入普通诊断的测试问题标记");
			await cli.until(/INVALID_ANSWER_STRUCTURE：回答不符合格式，未展示。/u);
			cli.send("/history");
			await cli.until(/问题：小甲 7.1 怎么配队/u);
			await cli.until(/INVALID_ANSWER_STRUCTURE：回答不符合格式，未展示。/u);
			await cli.close();
			const [trace, ...extra] = cli.diagnostics();
			assert.equal(extra.length, 0);
			assert.equal(trace.errorCode, "INVALID_ANSWER_STRUCTURE");
			assert.equal(trace.validationDetail, detail);
			assert.equal(trace.modelCalls, 3);
			assert.equal(trace.toolCalls, 2);
			assert.doesNotMatch(
				cli.output(),
				/validationDetail|引用：|不能进入普通诊断的模拟/u,
			);
			assert.doesNotMatch(cli.errors(), /不能进入普通诊断|未公开/u);
		},
	);
}

test(
	"真实 CLI＋Pi：先给带引用的配队思路，再给模型整理阵容，历史可恢复",
	linux,
	async (t) => {
		const cli = await open(t, "guides-observations");
		cli.send("小甲 7.1 通用配队");
		const readAnswer = async () => {
			await cli.until(/配队思路：\n队友选择：可以从队友丙入手/u);
			await cli.until(
				/配队建议（模型整理）：\n参考配队：角色甲＋队友丙＋队友丁＋队友戊/u,
			);
			await cli.until(/原文：https:\/\/example\.test\/guide/u);
		};
		await readAnswer();
		cli.send("/history");
		await cli.until(/问题：小甲 7.1 通用配队/u);
		await readAnswer();
		await cli.close();
		const [trace, ...extra] = cli.diagnostics();
		assert.equal(extra.length, 0);
		assert.equal(trace.status, "answered");
		assert.deepEqual([trace.modelCalls, trace.toolCalls], [3, 2]);
		assert.equal(cli.output().match(/引用：隔离攻略 G-01/gu)?.length, 2);
		assert.equal(cli.output().match(/角色甲的队友备选：队友丙。/gu)?.length, 2);
		assert.doesNotMatch(cli.output(), /资料不足：|INVALID_ANSWER_CONDITIONS/u);
		assert.doesNotMatch(cli.errors(), /队友丙/u);
	},
);

test("真实 CLI＋Pi：候选没读完仍拒绝把实际命中当资料不足", linux, async (t) => {
	const cli = await open(t, "guides-condition-unread");
	cli.send("小甲 7.1 通用配队；不公开的模拟问题标记");
	const terminal =
		/INVALID_ANSWER_CONDITIONS：回答与已确认条件或证据前提不符，未展示。/u;
	await cli.until(terminal);
	cli.send("/history");
	await cli.until(/问题：小甲 7.1 通用配队/u);
	await cli.until(terminal);
	await cli.close();
	const [trace, ...extra] = cli.diagnostics();
	assert.equal(extra.length, 0);
	assert.equal(trace.errorCode, "INVALID_ANSWER_CONDITIONS");
	assert.equal(
		trace.validationDetail,
		"insufficient_evidence:unread_candidates",
	);
	assert.deepEqual([trace.modelCalls, trace.toolCalls], [3, 2]);
	assert.doesNotMatch(
		cli.output(),
		/引用：|validationDetail|不公开的模拟模型原文/u,
	);
	assert.doesNotMatch(cli.errors(), /不公开的模拟|队友丙/u);
});

test("未持有或本轮排除目标时，两种模式仍展示原文参考", linux, async (t) => {
	const cli = await open(t, "guides-reference");
	cli.send("小甲 7.1 按库存配队我没有小甲");
	await cli.until(/参考配队：角色甲＋队友丙＋队友丁＋队友戊/u);
	await cli.until(/尚未持有角色「角色甲」/u);
	await cli.until(/原文：https:\/\/example\.test\/guide/u);
	cli.send("小甲 7.1 通用配队本轮不用小甲");
	await cli.until(/本轮排除角色「角色甲」/u);
	await cli.until(/原文：https:\/\/example\.test\/guide/u);
	await cli.close();
	assert.deepEqual(
		cli
			.diagnostics()
			.map(({ modelCalls, searchCalls }) => [modelCalls, searchCalls]),
		[
			[3, 0],
			[3, 0],
		],
	);
});

test("必需装备未知先给参考，确认持有后新轮可展示可用配队", linux, async (t) => {
	const cli = await open(t, "guides-equipment");
	cli.send("小甲 7.1 按库存配队；我有小甲、队友丙、队友丁、队友戊");
	await cli.until(/参考配队：/u);
	await cli.until(/必要装备持有状态未确认/u);
	await cli.until(/引用：隔离攻略 G-01/u);
	cli.send("小甲 7.1 按库存配队；我有小弓");
	await cli.until(/可用配队：/u);
	await cli.until(/适用前提：需持有试验弓/u);
	await cli.until(/引用：隔离攻略 G-01/u);
	await cli.close();
	const rows = cli.diagnostics();
	assert.deepEqual(
		rows.map(({ status, modelCalls, toolCalls }) => [
			status,
			modelCalls,
			toolCalls,
		]),
		[
			["answered", 3, 2],
			["answered", 3, 2],
		],
	);
	assert.equal(new Set(rows.map(({ turnId }) => turnId)).size, 2);
});
