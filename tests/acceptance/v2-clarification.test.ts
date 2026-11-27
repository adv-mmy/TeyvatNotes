import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, realpath, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { type TestContext, test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const options = { skip: process.platform !== "linux", timeout: 30_000 };
const fixture = fileURLToPath(
	new URL("../fixtures/pi-controlled-model.js", import.meta.url),
);

async function isolated(t: TestContext) {
	const parent = await realpath(tmpdir());
	const root = await mkdtemp(join(parent, "tn-v2-clarification-"));
	t.after(async () => {
		const absolute = await realpath(root);
		assert.equal(dirname(absolute), parent);
		assert.match(basename(absolute), /^tn-v2-clarification-[a-zA-Z0-9]+$/u);
		await rm(absolute, { recursive: true });
	});
	return root;
}

function cli(t: TestContext, root: string, scenario: string) {
	const child = spawn(process.execPath, [fixture, scenario], {
		env: { ...process.env, TEYVAT_SESSION_DIR: root, TEYVAT_DIAGNOSTICS: "1" },
	});
	const closed = once(child, "close");
	child.stdin.on("error", () => {});
	t.after(async () => {
		if (child.exitCode === null && child.signalCode === null)
			child.kill("SIGKILL");
		await closed;
	});
	let stdout = "";
	let stderr = "";
	let cursor = 0;
	child.stdout.setEncoding("utf8").on("data", (part: string) => {
		stdout += part;
	});
	child.stderr.setEncoding("utf8").on("data", (part: string) => {
		stderr += part;
	});
	return {
		send(line: string) {
			child.stdin.write(`${line}\n`);
		},
		async receive(pattern: RegExp) {
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
		diagnostics: () =>
			stderr
				.split("\n")
				.filter((line) => line.startsWith("{"))
				.map((line) => JSON.parse(line)),
		async exit() {
			child.stdin.end("/exit\n");
			assert.deepEqual(await closed, [0, null], stderr);
		},
	};
}

async function create(client: ReturnType<typeof cli>) {
	client.send("/new");
	const [, id] = await client.receive(/已创建会话 ([a-f0-9-]{36})/u);
	assert.ok(id);
	return id;
}

async function events(root: string, id: string) {
	return (await readFile(join(root, id, "turns.jsonl"), "utf8"))
		.trim()
		.split("\n")
		.filter(Boolean)
		.map((line) => JSON.parse(line));
}

test(
	"明确未持有跨进程保留，本轮禁用不跨轮；过滤后回读替代攻略，无解不编队",
	options,
	async (t) => {
		const root = await isolated(t);
		const first = cli(t, root, "guides-constraints");
		const id = await create(first);
		first.send("小甲 7.1 怎么配队？我没有角色乙");
		await first.receive(/队友丙携带试验弓搭配角色甲/u);
		await first.receive(/原文：https:\/\/example.test\/alternative/u);
		await first.exit();
		const resumed = cli(t, root, "guides-constraints");
		resumed.send(`/open ${id}`);
		await resumed.receive(/已打开会话/u);
		resumed.send("小甲 7.1 怎么配队？本轮不用小弓");
		await resumed.receive(/资料不足/u);
		resumed.send("小甲 7.1 怎么配队");
		await resumed.receive(/原文：https:\/\/example.test\/alternative/u);
		resumed.send("小甲 7.1 怎么配队？我有角色乙，本轮不用小弓");
		await resumed.receive(/原文：https:\/\/example.test\/guide/u);
		await resumed.exit();
		assert.deepEqual(
			resumed.diagnostics().map((r) => [r.status, r.toolCalls, r.searchCalls]),
			[
				["insufficient_evidence", 1, 0],
				["answered", 2, 0],
				["answered", 2, 0],
			],
		);
	},
);

test(
	"V2 装配从仓库术语资源读取角色别名，缺版本时仍不调用模型",
	options,
	async (t) => {
		const root = await isolated(t);
		const client = cli(t, root, "v2-local-terminology");
		const id = await create(client);
		client.send("奥萝塔怎么配队");
		await client.receive(/需要补充条件：请补充以下条件：版本：明确版本号/u);
		await client.exit();
		const rows = await events(root, id);
		assert.deepEqual(rows[1].clarification.fields, ["gameVersion"]);
		assert.equal(rows[1].clarification.scope.entityId, "奥黛塔");
		assert.deepEqual(
			client.diagnostics().map(({ modelCalls }) => modelCalls),
			[0],
		);
	},
);

test(
	"V2 补齐范围前零模型调用，模型失败后重新打开可显式继续原问题",
	options,
	async (t) => {
		const root = await isolated(t);
		const first = cli(t, root, "v2-fail");
		const id = await create(first);
		first.send("小甲按库存配队");
		await first.receive(/需要补充条件：请补充以下条件：版本：明确版本号/u);
		first.send("版本：7.1");
		await first.receive(/补充条件待确认：版本：7.1/u);
		first.send("确认");
		await first.receive(/需要补充条件：.*目标角色持有：持有.*队友：名字列表/u);
		first.send("目标角色持有：持有；队友：队友丙");
		await first.receive(/补充条件待确认：目标角色持有：持有；队友：队友丙/u);
		first.send("确认");
		await first.receive(/MODEL_FAILED/u);
		await first.exit();
		assert.deepEqual(
			first.diagnostics().map(({ modelCalls }) => modelCalls),
			[0, 0, 1],
		);
		const rows = await events(root, id);
		const originalId = rows[0].turnId;
		const confirmed = rows.find(
			({ type, conditions }) =>
				type === "conditions_confirmed" && conditions.holdings,
		);
		assert.deepEqual(confirmed.conditions, {
			holdings: { 角色甲: "owned" },
			teammates: ["队友丙"],
		});
		assert.equal(confirmed.clarification.questionTurnId, originalId);
		assert.equal(confirmed.clarification.state, "ready");
		assert.equal(rows.at(-1).outcome.code, "MODEL_FAILED");

		const resumed = cli(t, root, "v2-restored");
		resumed.send(`/open ${id}`);
		await resumed.receive(/已补齐。输入「继续」/u);
		assert.equal(resumed.diagnostics().length, 0);
		resumed.send("继续");
		await resumed.receive(/引用：TeyvatNotes 隔离测试样本/u);
		await resumed.exit();
		assert.deepEqual(
			resumed
				.diagnostics()
				.map(({ modelCalls, toolCalls }) => [modelCalls, toolCalls]),
			[[3, 2]],
		);
		const final = (await events(root, id)).at(-1);
		assert.equal(final.outcome.status, "answered");
		assert.equal(final.clarification, null);
	},
);

test(
	"V2 新问题切换时放弃未确认草稿，不套用原目标或库存",
	options,
	async (t) => {
		const root = await isolated(t);
		const client = cli(t, root, "v2-switch");
		const id = await create(client);
		client.send("小甲 7.1 按库存配队");
		await client.receive(/需要补充条件：.*目标角色持有/u);
		const pending = await events(root, id);
		client.send("确认前还能修改吗？");
		await client.receive(/未能明确识别补充条件，未保存/u);
		assert.deepEqual(await events(root, id), pending);
		client.send("目标角色持有：持有；队友：队友丙");
		await client.receive(/补充条件待确认/u);
		client.send("新问题：角色乙 7.2 怎么配队");
		await client.receive(/引用：TeyvatNotes 隔离测试样本/u);
		await client.exit();
		const rows = await events(root, id);
		assert.equal(
			rows.filter(({ type }) => type === "conditions_confirmed").length,
			0,
		);
		assert.equal(rows[2].type, "clarification_abandoned");
		assert.deepEqual(
			client.diagnostics().map(({ modelCalls }) => modelCalls),
			[0, 3],
		);
	},
);

test(
	"V2 切换版本、配队模式和角色：只继承已确认的全局库存，为每轮重建范围",
	options,
	async (t) => {
		const root = await isolated(t);
		const client = cli(t, root, "v2-inheritance");
		const id = await create(client);
		client.send(
			"小甲 7.1 按库存配队，限用装备，我有角色甲，我没有角色乙，本轮不用小弓",
		);
		await client.receive(/需要补充条件：.*队友：.*装备：/u);
		client.send("队友：队友丙；装备：无");
		await client.receive(/补充条件待确认：队友：队友丙；装备：明确为空/u);
		client.send("确认");
		await client.receive(/引用：TeyvatNotes 隔离测试样本/u);
		for (const input of [
			"小甲 7.2 按库存配队",
			"小甲 7.2 通用配队",
			"队友丙 7.2 通用配队",
		]) {
			client.send(input);
			await client.receive(/引用：TeyvatNotes 隔离测试样本/u);
		}
		await client.exit();
		assert.deepEqual(
			client
				.diagnostics()
				.map(({ status, modelCalls, toolCalls }) => [
					status,
					modelCalls,
					toolCalls,
				]),
			[
				["needs_clarification", 0, 0],
				["answered", 3, 2],
				["answered", 3, 2],
				["answered", 3, 2],
				["answered", 3, 2],
			],
		);
		const confirmed = (await events(root, id)).filter(
			({ type }) => type === "conditions_confirmed",
		);
		assert.ok(confirmed.length > 0);
		assert.deepEqual(confirmed.at(-1)?.conditions, {
			holdings: { 角色甲: "owned", 角色乙: "not_owned" },
			equipment: [],
			teammates: ["队友丙"],
		});
	},
);

test(
	"V2 放弃追问不会写入草稿，明确空库存不再追问也不调用模型",
	options,
	async (t) => {
		const root = await isolated(t);
		const client = cli(t, root, "v2-never");
		const id = await create(client);
		client.send("小甲 7.1 按库存配队");
		await client.receive(/需要补充条件/u);
		client.send("目标角色持有：持有");
		await client.receive(/补充条件待确认/u);
		client.send("放弃");
		await client.receive(/已放弃当前追问/u);
		client.send("小甲 7.1 按库存配队");
		await client.receive(/需要补充条件/u);
		client.send("队友：无");
		await client.receive(/补充条件待确认：队友：明确为空/u);
		client.send("确认");
		await client.receive(/已确认没有可用的队友/u);
		await client.exit();
		const rows = await events(root, id);
		const confirmations = rows.filter(
			({ type }) => type === "conditions_confirmed",
		);
		assert.deepEqual(
			confirmations.map(({ conditions }) => conditions),
			[{ teammates: [] }],
		);
		assert.deepEqual(
			client.diagnostics().map(({ modelCalls }) => modelCalls),
			[0, 0, 0],
		);
	},
);
