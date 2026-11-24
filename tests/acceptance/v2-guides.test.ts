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
			/引用：|索引依赖报错原文不可公开|按隔离样本给出配队建议/u,
		);
		const [trace] = cli.diagnostics();
		assert.deepEqual(
			[trace.toolCalls, trace.modelCalls],
			[toolCalls, modelCalls],
		);
	});
}

test(
	"按库存配队回读后追问必要装备，确认后新开一轮再检索和回读",
	linux,
	async (t) => {
		const cli = await open(t, "guides-equipment");
		cli.send("小甲 7.1 按库存配队");
		await cli.until(/目标角色持有：持有/u);
		cli.send("目标角色持有：持有；队友：队友丙");
		await cli.until(/补充条件待确认/u);
		cli.send("确认");
		await cli.until(/回读的配队方案需要「试验弓」/u);
		cli.send("装备：小弓");
		await cli.until(/补充条件待确认：装备：试验弓/u);
		cli.send("确认");
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
				["needs_clarification", 0, 0],
				["needs_clarification", 3, 2],
				["answered", 3, 2],
			],
		);
		assert.equal(new Set(rows.map(({ turnId }) => turnId)).size, 3);
	},
);
