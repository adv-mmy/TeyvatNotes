import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import pg from "pg";
import { ChromaVectorIndex } from "../../src/adapters/chroma/vector-index.js";
import { OllamaEmbedder } from "../../src/adapters/ollama/embedder.js";
import { createResponsesRuntime } from "../../src/adapters/pi/runtime.js";
import { PostgreSqlGuideRepository } from "../../src/adapters/postgres/guide-repository.js";
import { migrate } from "../../src/adapters/postgres/migrate.js";
import { BwikiSource, bwikiApi } from "../../src/adapters/web/bwiki-source.js";
import { parseWebGuide } from "../../src/adapters/web/metadata-parser.js";
import { createGuideTools } from "../../src/application/guide-tools.js";
import {
	IngestGuide,
	prepareGuide,
	webSupplement,
} from "../../src/application/ingest-guide.js";
import {
	GuideSnapshotChanged,
	type StoredGuide,
	type VectorWritePort,
} from "../../src/application/ports/guide-data.js";
import {
	permittedUrl,
	type WebSupplement,
} from "../../src/application/ports/guide-source.js";
import { GuideRetrieval } from "../../src/application/retrieve-guides.js";
import { TurnBudget } from "../../src/application/turn-budget.js";
import {
	readConfig,
	readDatabaseConfig,
	readGuideStorageConfig,
} from "../../src/bootstrap/config.js";
import { startCli } from "../../src/bootstrap/main.js";

const terminology = [
	{ category: "character", name: "钟离", aliases: ["岩王帝君"] },
	{ category: "character", name: "班尼特", aliases: [] },
	{ category: "character", name: "香菱", aliases: [] },
	{ category: "character", name: "行秋", aliases: [] },
	{ category: "weapon", name: "黑缨枪", aliases: ["史莱姆枪"] },
	{ category: "weapon", name: "西风长枪", aliases: [] },
	{ category: "artifact", name: "绝缘之旗印", aliases: [] },
	{ category: "artifact", name: "教官", aliases: [] },
] as const;
const sampleTeam = ["钟离", "班尼特", "香菱", "行秋"].map((characterId) => ({
	characterId,
	equipmentIds: [],
}));
const fixture = {
	title: "钟离配队验证",
	sourceUrl: "https://example.invalid/mock-team",
	entityId: "岩王帝君",
	guideKind: "team",
	gameVersions: { from: "7.2", through: "7.10" },
	applicability: { kind: "unrestricted" },
	body: "## 配队\n示例阵容为钟离、班尼特、香菱、行秋。\n\n| 前提 | 示例 |\n| --- | --- |\n| 装备 | 黑缨枪 |\n\n## 注意\n这段正文用于检验引用定位。",
};

const permit = {
	origin: "https://wiki.biligame.com",
	api: bwikiApi,
	path: "/ys/",
	reference: "仅用于隔离测试的虚构许可",
	access: "fetch_store_cite",
} as const;
const revision = {
	id: 81743,
	section: { index: "13", title: "阵容搭配", anchor: "阵容搭配" },
};
function wikiUrl(title: string) {
	return new URL(`/ys/${title}`, permit.origin).href;
}
function fakeWikiParse(
	title: string,
	content: string,
	inspected: boolean,
	id = revision.id,
) {
	return Response.json({
		parse: {
			title,
			revid: id,
			wikitext: { "*": content },
			...(inspected
				? {
						sections: [
							{ index: "2", line: "培养", anchor: "培养" },
							{
								index: revision.section.index,
								line: revision.section.title,
								anchor: revision.section.anchor,
							},
						],
					}
				: {}),
		},
	});
}
function wikiRequest(input: URL | RequestInfo, init?: RequestInit) {
	const request = new URL(String(input));
	assert.equal(request.origin + request.pathname, bwikiApi);
	assert.equal(init?.redirect, "error");
	assert.equal(init?.credentials, "omit");
	assert.equal(init?.method, "GET");
	const headers = new Headers(init?.headers);
	assert.equal(headers.get("authorization"), null);
	assert.equal(headers.get("cookie"), null);
	assert.equal(request.searchParams.get("format"), "json");
	return request.searchParams;
}

test("BWIKI 只访问固定原生 API，匹配标题和路径，按实返修订及章节单独计数", async () => {
	const title = "钟离/攻略";
	const url = wikiUrl(title);
	const scoped = { ...permit, path: "/ys/钟离/" };
	assert.equal(permittedUrl(url, scoped), url);
	const pages = [
		"https://127.0.0.1/ys/钟离/攻略",
		"http://wiki.biligame.com/ys/钟离/攻略",
		wikiUrl("薇斯纳/攻略"),
		`${permit.origin}/ys/钟离/%2e%2e/攻略`,
		`${permit.origin}/ys/钟离/%2F攻略`,
		`${permit.origin}/ys/钟离/%252e%252e/攻略`,
		`${url}?next=elsewhere`,
		`${url}#other`,
	];
	for (const page of pages) assert.equal(permittedUrl(page, scoped), undefined);
	const requests: Record<string, string>[] = [];
	const oldGuide = "钟离/旧版/攻略";
	const otherTitles = ["钟离", "钟离/培养素材", "钟离/其他资料"];
	const upstream = [...otherTitles, oldGuide, title];
	const source = new BwikiSource(async (input, init) => {
		const params = wikiRequest(input, init);
		requests.push(Object.fromEntries(params));
		if (params.get("action") === "opensearch")
			return Response.json(["钟离", upstream, [], upstream.map(wikiUrl)]);
		return fakeWikiParse(
			title,
			"== 阵容搭配 ==\n参考正文。",
			params.has("page"),
		);
	});
	const counts: string[] = [];
	const budget = {
		begin(op: string) {
			counts.push(op);
			return new AbortController().signal;
		},
	};
	const candidates = await source.search("钟离", budget);
	assert.deepEqual(candidates, [
		{ title, url },
		{ title: oldGuide, url: wikiUrl(oldGuide) },
		...otherTitles.map((title) => ({ title, url: wikiUrl(title) })),
	]);
	await assert.rejects(
		source.fetch({ title: "另一个角色/攻略", url }, scoped, budget),
		{ message: "SOURCE_RESTRICTED" },
	);
	assert.equal(
		source.canFetch({ ...scoped, api: `${permit.origin}/wiki/api.php` }),
		false,
	);
	assert.deepEqual(await source.fetch({ title, url }, scoped, budget), {
		title,
		content: "== 阵容搭配 ==\n参考正文。",
		revision,
	});
	assert.deepEqual(counts, ["discover", "read", "read"]);
	assert.deepEqual(
		requests.map(({ action, search, page, oldid, section }) => [
			action,
			search,
			page,
			oldid,
			section,
		]),
		[
			["opensearch", "钟离", undefined, undefined, undefined],
			["parse", undefined, title, undefined, undefined],
			[
				"parse",
				undefined,
				undefined,
				String(revision.id),
				revision.section.index,
			],
		],
	);
});

test("Wiki 章节保留版本与前提依据，只索引可核对正文；不发布冲突或模板与图片占位", () => {
	const title = "钟离/攻略";
	const header =
		"== 阵容搭配 ==\n适用版本：7.1\n目标角色：钟离\n攻略类型：配队\n适用条件：持有装备 史莱姆枪";
	const advice = "示例阵容：[[钟离]]＋班尼特＋香菱＋行秋。";
	const content = `${header}\n\n=== 示例 ===\n${advice}`;
	const parse = (content: string) =>
		parseWebGuide(
			{ title, content, revision },
			wikiUrl(title),
			"测试许可",
			terminology,
		);
	const parsed = parse(content);
	assert.equal(parsed.kind, "ready");
	if (parsed.kind !== "ready") return;
	assert.deepEqual(
		parsed.document.chunks.map((c) => c.body),
		["示例阵容：钟离＋班尼特＋香菱＋行秋。"],
	);
	assert.equal(parsed.document.content, content);
	assert.equal(
		parsed.document.webEvidence?.fields.versions?.excerpt,
		"适用版本：7.1",
	);
	assert.equal(
		parsed.document.webEvidence?.fields.conditions?.location,
		"阵容搭配 · 第 5 行",
	);
	assert.deepEqual(parsed.document.applicability, {
		kind: "known",
		conditions: ["持有装备 史莱姆枪"],
		requiredCharacterIds: [],
		requiredEquipmentIds: ["黑缨枪"],
	});
	for (const invalid of [
		content.replace("适用版本：7.1", "适用版本：7.1\n适用版本：7.2"),
		content.replace("目标角色：钟离", "目标角色：香菱"),
	])
		assert.equal(parse(invalid).kind, "incomplete");
	assert.equal(
		parse(
			content.replace(
				advice,
				"{{配队|钟离|班尼特|香菱|行秋}}\n[[文件:配队.png]]",
			),
		).kind,
		"unreadable",
	);
	const unknownPremise = parse(
		content.replace("持有装备 史莱姆枪", "未知\n必需装备：黑缨枪"),
	);
	assert.ok(unknownPremise.kind === "ready");
	assert.deepEqual(unknownPremise.document.applicability, { kind: "unknown" });
	assert.match(
		unknownPremise.document.chunks.map((c) => c.body).join("\n"),
		/必需装备：黑缨枪/u,
	);
	assert.equal(
		parse("== 阵容搭配 ==\n{{配队}}\n[[文件:配队.png]]").kind,
		"unreadable",
	);
	assert.deepEqual(parsed.document.webEvidence?.revision, revision);
	for (const missing of ["<!--\n适用版本：7.1\n-->", "适用版本：未知"]) {
		const unknown = parse(content.replace("适用版本：7.1", missing));
		assert.ok(unknown.kind === "ready");
		assert.equal(unknown.document.gameVersions, null);
		assert.equal(
			unknown.document.webEvidence?.unknownGameVersionReason,
			"not_stated",
		);
	}
	const fromTable = parse(
		content
			.replace("适用版本：7.1", "{|\n|-\n| 适用版本 || 7.1\n|}")
			.replace("\n攻略类型：配队", ""),
	);
	assert.ok(fromTable.kind === "ready");
	assert.deepEqual(fromTable.document.webEvidence?.fields.kind, {
		location: "阵容搭配 · 第 1 行",
		excerpt: "== 阵容搭配 ==",
	});
});

test("BWIKI 排版与角色模板可投影为带行号的阵容，不拼接备选栏位", () => {
	const title = "钟离/攻略";
	const team =
		"<b>阵容示例</b> || {{颜色|岩|{{图标/攻略|角色|岩王帝君}}}}＋<strong>{{图标/攻略|班尼特}}</strong>＋{{图标|角色|香菱}}＋{{图标/攻略|行秋}}";
	const container =
		'<div id="layout-root" style="width:90%;min-width:342px;margin-top:10px;margin-bottom:10px;overflow:auto">';
	const lines = [
		"== 阵容搭配 ==",
		container,
		"适用版本：7.1<br />适用条件：持有装备 史莱姆枪",
		"{{切换板|开始}}",
		"{{切换板|默认显示|配队一}}",
		"{{切换板|默认折叠|配队二}}",
		"{{切换板|显示内容}}",
		'<center><div class="tabber-table"><tabber>',
		"完整阵容=",
		'{| class="wikitable"',
		`| ${team}`,
		"|}",
		"|-|备选队友=",
		"{|",
		"! 角色",
		"| {{图标/攻略|角色|钟离}}",
		"| {{图标/攻略|行秋}}",
		"|}",
		"</tabber></div></center>",
		"{{切换板|内容结束}}",
		"{{切换板|折叠内容}}",
		"<b>",
		"班尼特＋香菱",
		"</b>",
		"{{切换板|内容结束}}",
		"{{切换板|结束}}",
		"</div>",
	];
	const parse = (content: string) =>
		parseWebGuide(
			{ title, content, revision },
			wikiUrl(title),
			"仅限测试",
			terminology,
		);
	const content = lines.join("\n");
	const result = parse(content);
	assert.ok(result.kind === "ready");
	assert.equal(result.document.content, content);
	assert.deepEqual(
		result.document.chunks
			.filter(({ section }) => section.endsWith("完整阵容"))
			.map(({ body }) => body),
		["阵容示例：钟离＋班尼特＋香菱＋行秋"],
	);
	assert.equal(
		result.document.chunks[0]?.location,
		"阵容搭配 / 配队一 / 完整阵容 · 第 11–11 行",
	);
	assert.deepEqual(result.document.webEvidence?.fields.roster, {
		location: "完整阵容 · 第 11 行",
		excerpt: `| ${team}`,
	});
	assert.equal(
		result.document.webEvidence?.fields.conditions?.location,
		"阵容搭配 · 第 3 行",
	);
	const unclosed = content.replace(
		"完整阵容=",
		"未闭合表格=\n{|\n| 推荐阵容 || 钟离＋班尼特＋香菱＋行秋\n|-|完整阵容=",
	);
	const resumed = parse(unclosed);
	assert.ok(resumed.kind === "ready");
	assert.deepEqual(
		resumed.document.chunks
			.filter(({ section }) => section.endsWith("完整阵容"))
			.map(({ body, location }) => ({ body, location })),
		[
			{
				body: "阵容示例：钟离＋班尼特＋香菱＋行秋",
				location: "阵容搭配 / 配队一 / 完整阵容 · 第 14–14 行",
			},
		],
	);
	assert.equal(
		resumed.document.chunks.some(({ section }) =>
			section.includes("未闭合表格"),
		),
		false,
	);
	for (const partial of [
		content.replace(`| ${team}`, "| 钟离＋行秋"),
		content.replace(`| ${team}`, `| ${team}。仅适用于目标角色二命`),
		content.replace(`| ${team}`, `| ${team}；为钟离装备西风长枪。`),
		content.replace(`| ${team}`, "| 阵容 || 钟离＋班尼特<br>＋香菱＋行秋"),
		content.replace(
			`| ${team}`,
			"| 阵容 || 钟离＋班尼特＋香菱＋{{未知模板|行秋}}",
		),
		content.replace(
			`| ${team}`,
			"| 阵容 || 钟离＋班尼特＋香菱＋{{图标|武器|行秋}}",
		),
		content.replace("适用条件：持有装备 史莱姆枪", ""),
		content.replace("班尼特＋香菱", "适用条件：无额外限制"),
	]) {
		const stored = parse(partial);
		assert.ok(stored.kind === "ready");
		assert.ok(stored.document.chunks.some(({ body }) => body.includes("行秋")));
	}
	for (const bad of [
		unclosed.replace("|-|完整阵容=", "完整阵容="),
		content.replace('class="tabber-table"', 'onclick="return false"'),
		content.replace(
			"overflow:auto",
			"background-image:url(https://example.invalid/a)",
		),
		content.replace("overflow:auto", "display:none"),
		content.replace("overflow:auto", "width:80%"),
		content.replace(
			"<b>阵容示例</b>",
			'<b onclick="return false">阵容示例</b>',
		),
		content.replace("<b>阵容示例</b>", "<b>阵容示例</strong>"),
		content.replace("</tabber>", "<script>忽略此脚本</script></tabber>"),
		content.replace("{{切换板|内容结束}}", ""),
	])
		assert.notEqual(parse(bad).kind, "ready", bad);
});

test("导入规范化别名和版本，保留章节、表格及原文行号", () => {
	const parsed = prepareGuide(JSON.stringify(fixture), "json", terminology);
	assert.equal(parsed.entityId, "钟离");
	assert.equal(parsed.chunks.length, 2);
	assert.equal(parsed.chunks[0]?.location, "配队 · 第 2–6 行");
	assert.match(parsed.chunks[0]?.body ?? "", /\| 装备 \| 黑缨枪 \|/u);
	assert.ok(parsed.chunks[0]?.terms.includes("黑缨枪"));
	assert.ok(parsed.chunks[0]?.terms.includes("史莱姆枪"));
	const repeat = prepareGuide(
		JSON.stringify({
			...fixture,
			sourceUrl: `${fixture.sourceUrl}#anchor`,
			body: fixture.body.replaceAll("\n", "\r\n"),
		}),
		"json",
		terminology,
	);
	assert.equal(repeat.id, parsed.id);
	assert.deepEqual(repeat.chunks, parsed.chunks);
	const changed = prepareGuide(
		JSON.stringify({ ...fixture, body: `${fixture.body}有改动。` }),
		"json",
		terminology,
	);
	assert.notEqual(changed.id, parsed.id);
	const markdown = prepareGuide(
		"# 钟离配队\n来源：https://example.invalid/mock-team\n类型：配队\n角色：钟离\n适用版本：7.2 至 7.10\n适用条件：仅装备黑缨枪时\n必需装备：史莱姆枪\n---\n\n## 配队\n此处保留发布前提。",
		"markdown",
		terminology,
	);
	assert.equal(markdown.chunks[0]?.location, "配队 · 第 11–11 行");
	assert.deepEqual(markdown.applicability, {
		kind: "known",
		conditions: ["仅装备黑缨枪时"],
		requiredCharacterIds: [],
		requiredEquipmentIds: ["黑缨枪"],
	});
});

test("导入拒绝不明确的适用条件、错误版本区间、未知专名和不安全链接", () => {
	for (const [overrides, code] of [
		[{ applicability: { kind: "unknown" } }, "GUIDE_METADATA_INCOMPLETE"],
		[
			{ gameVersions: { from: "7.10", through: "7.2" } },
			"GUIDE_METADATA_INCOMPLETE",
		],
		[{ entityId: "不存在的角色" }, "GUIDE_METADATA_INCOMPLETE"],
		[
			{ sourceUrl: "https://user:password@example.invalid/guide" },
			"GUIDE_INVALID",
		],
		[{ body: "## 只有标题" }, "GUIDE_INVALID"],
	] as const) {
		assert.throws(
			() =>
				prepareGuide(
					JSON.stringify({ ...fixture, ...overrides }),
					"json",
					terminology,
				),
			{ message: code },
		);
	}
});

test("向量不能完整回查时不发布，显式重导复用版本，已确认索引不重复向量化", async () => {
	const config = {
		id: "isolated",
		model: "qwen3-embedding:test",
		dimensions: 2,
	};
	const doc = prepareGuide(JSON.stringify(fixture), "json", terminology);
	let state: StoredGuide = { ...doc, indexed: false, published: false };
	let canRead = false;
	let embedCalls = 0;
	let calls: string[] = [];
	const stored = new Map<string, readonly number[]>();
	const vectors: VectorWritePort = {
		async hasAll(_config, ids) {
			return canRead && ids.every((id) => stored.has(id));
		},
		async nearest() {
			return [];
		},
		async vectors(_id, ids) {
			return canRead
				? new Map(
						ids.flatMap((id) => {
							const value = stored.get(id);
							return value ? [[id, value] as const] : [];
						}),
					)
				: new Map();
		},
		async upsert(_id, guide, values) {
			calls.push("upsert");
			assert.equal(guide.id, doc.id);
			for (const [key, value] of values) stored.set(key, value);
		},
	};
	const ingester = new IngestGuide(
		{
			async save(document) {
				assert.equal(document.id, doc.id);
				return state;
			},
			async forPublication() {
				return state;
			},
			async finishIndex() {
				calls.push("indexed");
				state = { ...state, indexed: true };
			},
			async publish() {
				calls.push("published");
				state = { ...state, published: true };
			},
		},
		{
			async embed() {
				embedCalls++;
				return [1, 0];
			},
		},
		vectors,
		config,
	);
	const signal = new AbortController().signal;
	await assert.rejects(ingester.import(doc, true, signal), {
		message: "INDEX_NOT_READY",
	});
	await assert.rejects(ingester.publish(doc.id, signal), {
		message: "INDEX_NOT_READY",
	});
	assert.deepEqual(calls, ["upsert"]);
	canRead = true;
	calls = [];
	assert.deepEqual(await ingester.import(doc, false, signal), {
		id: doc.id,
		published: false,
	});
	assert.deepEqual(calls, ["upsert", "indexed"]);
	await ingester.publish(doc.id, signal);
	assert.deepEqual(calls, ["upsert", "indexed", "published"]);
	assert.deepEqual(await ingester.import(doc, true, signal), {
		id: doc.id,
		published: true,
	});
	assert.equal(embedCalls, doc.chunks.length * 2);
});

test("Ollama 使用固定维度、不截断输入；拒绝坏向量并支持取消", async () => {
	let calls = 0;
	const payloads: unknown[] = [];
	const server = createServer(async (req, res) => {
		calls++;
		assert.equal(req.url, "/api/embed");
		const parts: Buffer[] = [];
		for await (const chunk of req) parts.push(chunk);
		payloads.push(JSON.parse(Buffer.concat(parts).toString("utf8")));
		res.setHeader("content-type", "application/json");
		res.end(JSON.stringify({ embeddings: calls === 1 ? [[1, 2]] : [[1]] }));
	});
	await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
	try {
		const { port } = server.address() as AddressInfo;
		const embedder = new OllamaEmbedder(new URL(`http://127.0.0.1:${port}`), {
			id: "isolated",
			model: "qwen3-embedding:test",
			dimensions: 2,
		});
		const signal = new AbortController().signal;
		assert.deepEqual(
			await embedder.embed("中文样本", "isolated", signal),
			[1, 2],
		);
		assert.deepEqual(payloads[0], {
			model: "qwen3-embedding:test",
			input: "中文样本",
			dimensions: 2,
			truncate: false,
		});
		await assert.rejects(embedder.embed("坏响应", "isolated", signal), {
			message: "EMBEDDING_UNAVAILABLE",
		});
		await assert.rejects(
			embedder.embed("取消后不发送", "isolated", AbortSignal.abort()),
		);
		assert.equal(calls, 2);
	} finally {
		await new Promise<void>((resolve, reject) =>
			server.close((error) => (error ? reject(error) : resolve())),
		);
	}
});

const localEmbeddingStub = process.env.TEYVAT_V2_TEST_EMBED_STUB === "1";
test(`隔离 Compose：真实迁移、CLI 导入与发布、混合检索及回读${localEmbeddingStub ? "（Ollama 协议替身）" : "（真实 Ollama）"}`, {
	skip: process.env.TEYVAT_V2_TEST_ISOLATED !== "1",
	timeout: 180_000,
}, async (t) => {
	const env = { ...process.env };
	const db = await readDatabaseConfig(env);
	const stub = localEmbeddingStub
		? createServer(async (req, res) => {
				const parts: Buffer[] = [];
				for await (const chunk of req) parts.push(chunk);
				const request = JSON.parse(Buffer.concat(parts).toString("utf8"));
				const vector = Array.from(
					{ length: Number(request.dimensions) },
					(_, i) => (i === 0 ? 1 : 0),
				);
				res.setHeader("content-type", "application/json");
				res.end(JSON.stringify({ embeddings: [vector] }));
			})
		: undefined;
	if (stub) {
		await new Promise<void>((resolve) => stub.listen(0, "127.0.0.1", resolve));
		env.TEYVAT_OLLAMA_URL = `http://127.0.0.1:${(stub.address() as AddressInfo).port}`;
		env.TEYVAT_EMBEDDING_MODEL = "qwen3-embedding:test-stub";
	}
	const temp = await mkdtemp(join(tmpdir(), "teyvatnotes-storage-check-"));
	const storage = readGuideStorageConfig(env);
	const pool = new pg.Pool(db);
	const repository = new PostgreSqlGuideRepository(pool, storage.embedding.id);
	const embeddings = new OllamaEmbedder(storage.ollama, storage.embedding);
	const vectorIndex = new ChromaVectorIndex(
		storage.chroma,
		storage.collection,
		storage.embedding,
	);
	const cliPath = fileURLToPath(
		new URL("../../src/bootstrap/main.js", import.meta.url),
	);
	const cli = async (...args: string[]) =>
		(
			await promisify(execFile)(process.execPath, [cliPath, ...args], {
				env,
			})
		).stdout;
	const signal = AbortSignal.timeout(140_000);
	const major = localEmbeddingStub ? "993" : "994";
	const filter = {
		entityId: "钟离",
		gameVersion: `${major}.9`,
		guideKind: "team",
	} as const;
	const scope = {
		entityId: filter.entityId,
		guideKind: filter.guideKind,
		versionScope: { kind: "exact", version: filter.gameVersion },
		goal: "配队",
		teamMode: "general",
		playerConstraints: {
			targetHolding: "unknown",
			teammates: { kind: "unknown" },
			equipment: { kind: "unknown" },
		},
	} as const;
	try {
		assert.match(await cli("db", "migrate"), /已初始化攻略库|已执行/u);
		assert.match(await cli("db", "migrate"), /无需重复/u);
		// 使用独立的 schema 验证历史校验和，不更改已应用的迁移记录。
		const schema = `migration_${randomUUID().replaceAll("-", "")}`;
		await pool.query(`CREATE SCHEMA ${schema}`);
		const legacy = new pg.Pool({ ...db, options: `-c search_path=${schema}` });
		try {
			await legacy.query(
				"CREATE TABLE guide_schema_migration(id text PRIMARY KEY, checksum text NOT NULL)",
			);
			const hash = (s: string) => createHash("sha256").update(s).digest("hex");
			for (const name of [
				"001_guide_store",
				"002_web_guide_evidence",
				"003_nullable_guide_game_versions",
			]) {
				const lf = (
					await readFile(
						new URL(`../../../migrations/${name}.sql`, import.meta.url),
						"utf8",
					)
				).replaceAll("\r\n", "\n");
				await legacy.query(lf);
				await legacy.query(
					"INSERT INTO guide_schema_migration VALUES ($1,$2)",
					[name, hash(lf.replaceAll("\n", "\r\n"))],
				);
			}
			await legacy.query(`
				INSERT INTO guide_source(source_url, title, source_type)
				VALUES ('https://example.invalid/migration-fixture', '隔离升级样本', 'manual');
				INSERT INTO guide_version(id, source_id, title, content, content_hash,
					processing_version, entity_id, guide_kind, game_from, game_through,
					game_from_key, game_through_key, applicability, status)
				SELECT 'legacy-known', id, title, '隔离正文', 'legacy-hash', 'team-1', '钟离',
					'team', '7.1', '7.2', 7001000, 7002000, '{"kind":"unrestricted"}', 'published'
				FROM guide_source WHERE source_url='https://example.invalid/migration-fixture';
				INSERT INTO guide_source(source_url, title, source_type, permission_ref)
				VALUES ('https://example.invalid/legacy-web', '隔离联网样本', 'web', 'fixture');
				INSERT INTO guide_version(id, source_id, title, content, content_hash,
					processing_version, entity_id, guide_kind, applicability, status, web_evidence)
				SELECT 'legacy-web', id, title, '隔离联网正文', 'legacy-web-hash', 'bwiki-team-3',
					'钟离', 'team', '{"kind":"unrestricted"}', 'published',
					'{"permission":"fixture","parser":"bwiki-team-3","fields":{"entity":{},"kind":{},"conditions":{}},"unknownGameVersionReason":"not_stated"}'
				FROM guide_source WHERE source_url='https://example.invalid/legacy-web';
			`);
			assert.equal(await migrate(legacy, signal), true);
			assert.equal(await migrate(legacy, signal), false);
			assert.deepEqual(
				(
					await legacy.query(
						`SELECT id, game_from, game_through, status, web_evidence->>'parser' AS parser
						 FROM guide_version ORDER BY id`,
					)
				).rows,
				[
					{
						id: "legacy-known",
						game_from: "7.1",
						game_through: "7.2",
						status: "published",
						parser: null,
					},
					{
						id: "legacy-web",
						game_from: null,
						game_through: null,
						status: "published",
						parser: "bwiki-team-3",
					},
				],
			);
			for (const [id, constraint] of [
				["legacy-known", "guide_version_publishable_applicability"],
				["legacy-web", "guide_web_evidence_valid"],
			]) {
				await assert.rejects(
					legacy.query(
						`UPDATE guide_version SET applicability='{"kind":"unknown"}' WHERE id=$1`,
						[id],
					),
					{ code: "23514", constraint },
				);
			}
			await legacy.query(
				"UPDATE guide_schema_migration SET checksum=$1 WHERE id='002_web_guide_evidence'",
				[hash("SELECT 42;")],
			);
			await assert.rejects(migrate(legacy, signal), {
				message: "MIGRATION_CHECKSUM_MISMATCH",
			});
		} finally {
			await legacy.end();
		}
		const retrieval = new GuideRetrieval({
			repository,
			embeddings,
			vectorIndex,
		});
		assert.deepEqual(
			await retrieval.searchGuides(
				{ ...scope, versionScope: { kind: "exact", version: "991.1" } },
				"钟离配队黑缨枪",
				signal,
			),
			{ kind: "miss" },
		);
		const actual = {
			...fixture,
			entityId: "钟离",
			sourceUrl: `https://example.invalid/mock-team/${randomUUID()}`,
			gameVersions: { from: `${major}.2`, through: `${major}.10` },
		};
		const file = join(temp, "guide.json");
		await writeFile(file, JSON.stringify(actual), "utf8");
		const result = await cli("guides", "import", file);
		assert.match(result, /待发布攻略/u);
		const id = result.match(/[0-9a-f]{64}/u)?.[0];
		assert.ok(id);
		const pending = await repository.forPublication(
			id,
			storage.embedding.id,
			signal,
		);
		assert.ok(pending?.indexed && !pending.published);
		const stale = await repository.snapshot(filter, signal);
		assert.equal(
			(await repository.eligibleIds(filter, stale, null, 100, signal)).ids.some(
				(key) => pending.chunks.some((chunk) => chunk.id === key),
			),
			false,
		);
		assert.match(await cli("guides", "publish", id), /已发布/u);
		await assert.rejects(
			repository.assertSnapshot(filter, stale, signal),
			GuideSnapshotChanged,
		);
		const afterPublication = await repository.snapshot(filter, signal);
		assert.match(
			await cli("guides", "import", file, "--publish"),
			new RegExp(`已发布攻略：${id}`, "u"),
		);
		const snapshot = await repository.snapshot(filter, signal);
		assert.deepEqual(snapshot, afterPublication);
		const key = await repository.keywords(
			filter,
			snapshot,
			"钟离 黑缨枪",
			null,
			1,
			signal,
		);
		assert.ok(key.ids[0] && key.next, "关键词需正确分页");
		const next = await repository.keywords(
			filter,
			snapshot,
			"钟离 黑缨枪",
			key.next,
			1,
			signal,
		);
		assert.ok(next.ids[0] && key.ids[0] !== next.ids[0]);
		const semantic = await vectorIndex.nearest(
			snapshot.embeddingId,
			await embeddings.embed(fixture.body, snapshot.embeddingId, signal),
			100,
			signal,
			filter,
		);
		assert.ok(
			semantic.some(({ id }) =>
				pending.chunks.some((chunk) => chunk.id === id),
			),
		);
		const outcome = await retrieval.searchGuides(
			scope,
			"钟离配队黑缨枪",
			signal,
		);
		assert.equal(outcome.kind, "hit");
		if (outcome.kind !== "hit") return;
		const candidate = outcome.candidates[0];
		assert.ok(candidate);
		const read = await retrieval.readGuide(scope, candidate, signal);
		assert.equal(read.kind, "complete");
		if (read.kind === "complete") {
			assert.match(
				read.window.chunk.sourceUrl,
				/^https:\/\/example\.invalid\/mock-team\//u,
			);
			assert.match(read.window.chunk.location, /第 \d+–\d+ 行/u);
			assert.match(read.window.body, /示例阵容|引用定位/u);
		}
		assert.equal(
			(
				await retrieval.searchGuides(
					{ ...scope, versionScope: { kind: "exact", version: "991.1" } },
					"钟离配队黑缨枪",
					signal,
				)
			).kind,
			"miss",
		);
		const disconnected = new GuideRetrieval({
			repository,
			embeddings,
			vectorIndex: new ChromaVectorIndex(
				new URL("http://127.0.0.1:1"),
				storage.collection,
				storage.embedding,
			),
		});
		assert.deepEqual(
			await disconnected.searchGuides(scope, "钟离配队黑缨枪", signal),
			{
				kind: "error",
				code: "RETRIEVAL_UNAVAILABLE",
			},
		);
		// 不注入问答工厂，走正式 CLI 装配。仅在 HTTP 边界替换付费服务。
		const originalFetch = globalThis.fetch;
		let requests = 0;
		let scripted: ((request: number, result: unknown) => object) | undefined;
		let scriptError: unknown;
		const respond = (item: object, request: number) =>
			new Response(
				[
					{ type: "response.output_item.done", output_index: 0, item },
					{
						type: "response.completed",
						response: {
							id: `resp_${request}`,
							status: "completed",
							output: [item],
						},
					},
				]
					.map((event) => `data: ${JSON.stringify(event)}\n\n`)
					.join(""),
				{ headers: { "content-type": "text/event-stream" } },
			);
		t.mock.method(
			globalThis,
			"fetch",
			async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
				const request = new Request(input, init);
				if (request.url !== "https://model.invalid/responses") {
					assert.ok(
						[storage.chroma.origin, storage.ollama.origin].includes(
							new URL(request.url).origin,
						),
					);
					return originalFetch(request);
				}
				requests++;
				const payload = await request.json();
				assert.equal(
					request.headers.get("authorization"),
					"Bearer isolated-test-key",
				);
				assert.equal(payload.model, "deepseek-flash");
				assert.equal(payload.max_output_tokens, 8192);
				assert.deepEqual(payload.reasoning, { effort: "none" });
				assert.equal(payload.previous_response_id, undefined);
				assert.deepEqual(
					payload.tools.map((tool: { name: string }) => tool.name).sort(),
					["read_guide", "search_guides", "search_web_guides"],
				);
				const last = payload.input.findLast(
					(item: { type: string }) => item.type === "function_call_output",
				);
				const content = last
					? JSON.parse(
							typeof last.output === "string"
								? last.output
								: last.output[0].text,
						)
					: undefined;
				if (requests > 1) assert.equal(last.call_id, `call_${requests - 1}`);
				if (scripted) {
					try {
						return respond(scripted(requests, content), requests);
					} catch (error) {
						scriptError = error;
						throw error;
					}
				}
				const tool =
					!content || content.status === "indexed"
						? "search_guides"
						: content.status === "miss" && content.reason !== "recheck_miss"
							? "search_web_guides"
							: content.status === "hit"
								? "read_guide"
								: undefined;
				const candidate =
					content?.candidates?.find((item: { preview: string }) =>
						item.preview.includes("示例阵容"),
					) ?? content?.candidates?.[0];
				const item = tool
					? {
							type: "function_call",
							id: `fc_${requests}`,
							call_id: `call_${requests}`,
							name: tool,
							arguments: JSON.stringify(
								tool !== "read_guide"
									? { query: "钟离配队" }
									: { candidateId: candidate?.candidateId },
							),
						}
					: {
							type: "message",
							id: "msg_3",
							role: "assistant",
							content: [
								{
									type: "output_text",
									text: JSON.stringify(
										content.status !== "complete"
											? {
													recommendations: [],
													gaps: [
														{
															kind: "insufficient_evidence",
															text: "模型不能替应用决定失败原因",
														},
													],
												}
											: {
													recommendations: [
														{
															team: sampleTeam,
															evidenceIds: [content.evidenceId],
														},
													],
													gaps: [],
												},
									),
								},
							],
						};
				return respond(item, requests);
			},
		);
		const secret = join(temp, "model-key");
		await writeFile(secret, "isolated-test-key", { mode: 0o600 });
		const playCli = async (
			steps: readonly (readonly [output: RegExp, input: string])[],
			supplement?: WebSupplement,
			toolLimit = 8,
			modelLimit = 6,
			currentVersion = "",
		) => {
			requests = 0;
			scriptError = undefined;
			const stdin = new PassThrough();
			const stdout = new PassThrough();
			let transcript = "";
			let next = 0;
			let cursor = 0;
			stdout.on("data", (data) => {
				transcript += data.toString();
				const step = steps[next];
				if (!step) return;
				const [output, input] = step;
				const match = transcript.slice(cursor).match(output);
				if (!match) return;
				next++;
				cursor += (match.index ?? 0) + match[0].length;
				setImmediate(() =>
					input === "/exit" ? stdin.end("/exit\n") : stdin.write(`${input}\n`),
				);
			});
			const timer = setTimeout(() => stdin.end(), 30_000);
			try {
				await startCli(
					{
						...env,
						TEYVAT_SESSION_DIR: join(temp, "sessions"),
						TEYVAT_MODEL_BASE_URL: "https://model.invalid",
						TEYVAT_MODEL_ID: "deepseek-flash",
						TEYVAT_MODEL_API_KEY_FILE: secret,
						TEYVAT_TOOL_CALLS: String(toolLimit),
						TEYVAT_MODEL_CALLS: String(modelLimit),
						TEYVAT_CURRENT_GAME_VERSION: currentVersion,
					},
					supplement
						? async (context) => ({
								mode: "guide",
								terminology,
								data: { repository, embeddings, vectorIndex },
								supplement,
								runtime: await createResponsesRuntime({
									...context,
									key: "isolated-test-key",
									baseUrl: "https://model.invalid",
									model: "deepseek-flash",
									contextWindow: 65536,
									maxTokens: 8192,
								}),
							})
						: undefined,
					{ input: stdin, output: stdout },
				);
			} finally {
				clearTimeout(timer);
			}
			if (scriptError) throw scriptError;
			assert.equal(next, steps.length, transcript);
			return transcript;
		};
		const runQuestion = (
			gameVersion: string,
			supplement?: WebSupplement,
			toolLimit = 8,
			modelLimit = 6,
		) =>
			playCli(
				[
					[/命令：/u, "/new"],
					[/已创建会话/u, `钟离 ${gameVersion} 通用配队`],
					[/原文：|[A-Z_]{4,}：|需要补充条件：|资料不足：/u, "/exit"],
				],
				supplement,
				toolLimit,
				modelLimit,
			);
		const transcript = await runQuestion(filter.gameVersion);
		assert.match(transcript, /参考配队：钟离＋班尼特＋香菱＋行秋/u);
		assert.match(transcript, /原文：https:\/\/example\.invalid\//u);
		assert.equal(requests, 3);
		const gearDoc = prepareGuide(
			JSON.stringify({
				...fixture,
				sourceUrl: `https://example.invalid/gear/${randomUUID()}`,
				gameVersions: { from: "975.1", through: "975.1" },
				applicability: {
					kind: "known",
					conditions: [],
					requiredCharacterIds: [],
					requiredEquipmentIds: ["黑缨枪"],
				},
			}),
			"json",
			terminology,
		);
		const ingest = new IngestGuide(
			repository,
			embeddings,
			vectorIndex,
			storage.embedding,
		);
		await ingest.import(gearDoc, true, signal);
		const gearAnswer = await runQuestion("975.1", async () =>
			assert.fail("已发布攻略不应触发补查"),
		);
		assert.match(gearAnswer, /需持有装备「黑缨枪」/u);
		assert.match(gearAnswer, /原文：https:\/\/example\.invalid\/gear\//u);
		assert.equal(requests, 3);
		// 独立版本模拟已就绪的两块只剩一个向量。不删除已有向量，验证后补回此样本的索引。
		const drift = prepareGuide(
			JSON.stringify({
				...fixture,
				sourceUrl: `https://example.invalid/drift/${randomUUID()}`,
				gameVersions: { from: "975.2", through: "975.2" },
			}),
			"json",
			terminology,
		);
		const saved = await repository.save(drift, storage.embedding, signal);
		assert.equal(saved.chunks.length, 2);
		const one = saved.chunks[0];
		assert.ok(one);
		await vectorIndex.upsert(
			storage.embedding.id,
			{ ...saved, chunks: [one] },
			new Map([
				[
					one.id,
					await embeddings.embed(one.body, storage.embedding.id, signal),
				],
			]),
			signal,
		);
		await repository.finishIndex(saved.id, storage.embedding.id, signal);
		await repository.publish(saved.id, storage.embedding.id, signal);
		try {
			const partial = await runQuestion("975.2", async () =>
				assert.fail("索引故障不应触发补查"),
			);
			assert.match(partial, /RETRIEVAL_UNAVAILABLE/u);
			assert.doesNotMatch(partial, /原文：/u);
			assert.equal(requests, 1);
		} finally {
			await ingest.import(drift, true, signal);
		}
		const title = `钟离/${randomUUID()}/攻略`;
		const url = wikiUrl(title);
		// 复用隔离卷时选一个尚无样本的范围，不清表，也不靠随机数避开旧命中。
		const freeRange = await pool.query<{ minor: number }>(`
			SELECT minor FROM generate_series(0,999) AS minor
			WHERE NOT EXISTS (SELECT 1 FROM guide_version WHERE game_from_key <= 978000002 + minor * 1000
				AND game_through_key >= 978000001 + minor * 1000)
			ORDER BY minor LIMIT 1`);
		assert.ok(freeRange.rows[0]);
		let webVersion = `978.${freeRange.rows[0].minor}.1`;
		const webScope = {
			versionScope: { kind: "exact", version: webVersion },
		} as const;
		const content =
			"== 阵容搭配 ==\n适用游戏版本：" +
			webVersion +
			"\n目标角色：钟离\n攻略类型：配队\n适用条件：无额外限制\n\n=== 示例 ===\n示例阵容为钟离、班尼特、香菱、行秋。";
		let wikitext = content;
		let searches = 0;
		let fetches = 0;
		let empty = false;
		let restricted = false;
		let profileFirst = false;
		let fault:
			| ((
					isSearch: boolean,
					isRead: boolean,
					signal: AbortSignal | null | undefined,
			  ) => Response | Promise<Response> | undefined)
			| undefined;
		const options = {
			source: new BwikiSource(async (input, init) => {
				const params = wikiRequest(input, init);
				const isSearch = params.get("action") === "opensearch";
				const isRead = params.has("oldid");
				if (isSearch) searches++;
				else fetches++;
				const injected = fault?.(isSearch, isRead, init?.signal);
				if (injected) return injected;
				if (isSearch) {
					assert.equal(params.get("search"), "钟离");
					if (empty) return Response.json(["钟离", [], [], []]);
					if (profileFirst) {
						return Response.json([
							"钟离",
							["钟离", title],
							[],
							[wikiUrl("钟离"), url],
						]);
					}
					return Response.json([
						"钟离",
						restricted ? ["其他"] : ["其他", title, title],
						[],
						restricted
							? ["https://other.invalid/disallowed"]
							: ["https://other.invalid/disallowed", url, url],
					]);
				}
				if (params.has("page")) assert.equal(params.get("page"), title);
				else {
					assert.equal(params.get("oldid"), String(revision.id));
					assert.equal(params.get("section"), revision.section.index);
				}
				return fakeWikiParse(
					title,
					isRead ? wikitext : "== 阵容搭配 ==\n目录定位样本",
					!isRead,
				);
			}),
			permits: [permit],
			parse: parseWebGuide,
			terms: terminology,
			ingest: new IngestGuide(
				repository,
				embeddings,
				vectorIndex,
				storage.embedding,
			),
			archive: repository.archiveIncomplete.bind(repository),
		};
		const supplement = webSupplement(options);
		const gateBudget = new TurnBudget(
			{ ...readConfig({}).limits, toolCalls: 12 },
			signal,
		);
		const envTools = createGuideTools(
			retrieval,
			supplement,
		)({
			scope: { ...scope, ...webScope, scopeId: "fixture" },
			turnId: "fixture",
			budget: gateBudget,
		});
		const invoke = async (...pairs: [string, string][]) => {
			const calls = pairs.map(([name, query]) => ({
				id: randomUUID(),
				name,
				arguments: { query },
			}));
			const result = await envTools.executeBatch({
				calls,
				rejected: [],
				signal,
			});
			envTools.confirmDelivery(calls.map((c) => c.id));
			return result;
		};
		try {
			for (let i = 0; i < 2; i++) {
				const mixed = await invoke(
					["search_guides", "钟离"],
					["search_web_guides", "钟离配队"],
				);
				assert.ok(
					mixed[1] && !mixed[1].ok && mixed[1].code === "TOOL_NOT_ALLOWED",
				);
			}
			const invalid = await invoke([
				"search_web_guides",
				"钟离 site:other.invalid",
			]);
			assert.ok(
				invalid[0] &&
					!invalid[0].ok &&
					invalid[0].code === "INVALID_TOOL_ARGUMENT",
			);
			assert.deepEqual([searches, fetches], [0, 0]);
		} finally {
			envTools.close();
			gateBudget.close();
		}
		const check = async (call: WebSupplement, fetchLimit = 3) => {
			const budget = new TurnBudget(
				{ ...readConfig({}).limits, fetchCalls: fetchLimit },
				signal,
			);
			try {
				return await call(
					{ ...scope, ...webScope, scopeId: "fixture" },
					budget,
					budget.signal,
				);
			} finally {
				budget.close();
			}
		};
		assert.equal(
			await check(webSupplement({ ...options, permits: [] })),
			"source_restricted",
		);
		assert.deepEqual([searches, fetches], [0, 0]);
		await assert.rejects(check(supplement, 0), { message: "BUDGET_EXHAUSTED" });
		await assert.rejects(check(supplement, 1), { message: "BUDGET_EXHAUSTED" });
		assert.deepEqual([searches, fetches], [0, 0]);
		empty = true;
		assert.equal(await check(supplement), "no_results");
		empty = false;
		restricted = true;
		assert.equal(await check(supplement), "source_restricted");
		restricted = false;
		assert.equal(
			await check(
				webSupplement({
					...options,
					permits: [{ ...permit, api: `${permit.origin}/wiki/api.php` }],
				}),
			),
			"source_restricted",
		);
		assert.deepEqual(
			[searches, fetches],
			[2, 0],
			"不读取越权候选，也不为不受支持的 API 发起搜索",
		);
		for (const op of ["search", "fetch"] as const) {
			const budget = new TurnBudget(
				{ ...readConfig({}).limits, searchMs: 50, fetchMs: 50 },
				signal,
			);
			let observed: AbortSignal | undefined;
			fault = (isSearch, _isRead, active) => {
				if (isSearch !== (op === "search")) return;
				observed = active ?? undefined;
				return new Promise<Response>(() => {});
			};
			try {
				await assert.rejects(
					supplement(
						{ ...scope, ...webScope, scopeId: "fixture" },
						budget,
						budget.signal,
					),
					{ message: "TOOL_TIMEOUT" },
				);
				assert.equal(observed?.aborted, true);
				assert.equal(budget.summary().searchCalls, 1);
				assert.equal(budget.summary().fetchCalls, Number(op === "fetch"));
			} finally {
				fault = undefined;
				budget.close();
			}
		}
		for (const bad of [
			() => new Response("<html>错误</html>", { status: 567 }),
			() => new Response("<html>错误</html>"),
			() => Response.redirect("https://other.invalid/stop"),
			() => Response.redirect(`${permit.origin}/ys/other`),
			() => Response.json({ error: { code: "badvalue" } }),
		]) {
			const count = fetches;
			fault = (isSearch) => (isSearch ? bad() : undefined);
			await assert.rejects(check(supplement), {
				message: "WEB_SOURCE_UNAVAILABLE",
			});
			assert.equal(fetches, count);
		}
		for (const bad of [
			() => fakeWikiParse(title, content, false, revision.id + 1),
			() => fakeWikiParse(title, "== 培养 ==\n其他章节", false),
		]) {
			const count = fetches;
			fault = (_isSearch, isRead) => (isRead ? bad() : undefined);
			await assert.rejects(check(supplement), {
				message: "WEB_SOURCE_UNAVAILABLE",
			});
			assert.equal(fetches - count, 2);
		}
		fault = undefined;
		wikitext = "== 阵容搭配 ==\n{{配队|占位}}";
		assert.equal(await check(supplement), "no_readable_body");
		wikitext = content.replace(webVersion, "1.0");
		assert.equal(await check(supplement), "scope_mismatch");
		wikitext = content.replace("目标角色：钟离", "");
		profileFirst = true;
		const callsBeforeIncomplete = [searches, fetches];
		assert.equal(await check(supplement), "metadata_incomplete");
		assert.deepEqual(
			[
				searches - (callsBeforeIncomplete[0] ?? 0),
				fetches - (callsBeforeIncomplete[1] ?? 0),
			],
			[1, 2],
		);
		assert.equal(
			(
				await pool.query(
					"SELECT 1 FROM guide_incomplete_page WHERE source_url=$1",
					[url],
				)
			).rowCount,
			1,
		);
		assert.equal(
			(
				await pool.query("SELECT 1 FROM guide_source WHERE source_url=$1", [
					url,
				])
			).rowCount,
			0,
		);
		wikitext = content;
		const notVisible: VectorWritePort = {
			hasAll: async () => false,
			upsert: async () => {},
			vectors: async () => new Map(),
			nearest: vectorIndex.nearest.bind(vectorIndex),
		};
		const notIndexed = webSupplement({
			...options,
			ingest: new IngestGuide(
				repository,
				embeddings,
				notVisible,
				storage.embedding,
			),
		});
		await assert.rejects(check(notIndexed), { message: "INDEX_NOT_READY" });
		assert.deepEqual(
			(
				await pool.query(
					"SELECT status, ready FROM guide_version JOIN guide_index_checkpoint ON version_id = id WHERE source_id = (SELECT id FROM guide_source WHERE source_url = $1)",
					[url],
				)
			).rows,
			[{ status: "pending", ready: false }],
		);
		searches = 0;
		fetches = 0;
		const first = await runQuestion(webVersion, supplement);
		assert.ok(first.includes(`原文：${url}`), first);
		assert.match(first, /参考配队：钟离＋班尼特＋香菱＋行秋/u);
		assert.match(first, /示例阵容为钟离、班尼特、香菱、行秋。/u);
		assert.equal(first.match(/正在执行工具/gu)?.length, 4);
		assert.deepEqual([requests, searches, fetches], [5, 1, 2]);
		const second = await runQuestion(webVersion, supplement);
		assert.ok(second.includes(`原文：${url}`), second);
		assert.equal(second.match(/正在执行工具/gu)?.length, 2);
		assert.deepEqual([requests, searches, fetches], [3, 1, 2]);
		profileFirst = false;
		const published = await pool.query<{
			web_evidence: { revision: typeof revision };
		}>(
			"SELECT web_evidence FROM guide_version JOIN guide_source ON source_id = guide_source.id WHERE source_url=$1 AND status='published'",
			[url],
		);
		assert.equal(published.rowCount, 1);
		assert.deepEqual(published.rows[0]?.web_evidence.revision, revision);
		const nextVersion = webVersion.replace(/\.1$/u, ".2");
		wikitext = content.replace(webVersion, nextVersion);
		webVersion = nextVersion;
		let rechecks = 0;
		const missedAgain = await runQuestion(webVersion, async () => {
			rechecks++;
			return "indexed";
		});
		assert.match(missedAgain, /资料已入库，但重新检索后没有找到/u);
		assert.doesNotMatch(missedAgain, /原文：/u);
		assert.deepEqual([requests, rechecks], [4, 1]);
		for (const [tools, models] of [
			[2, 6],
			[8, 2],
			[3, 5],
			[4, 4],
		] as const) {
			const limited = await runQuestion(webVersion, supplement, tools, models);
			assert.match(limited, /BUDGET_EXHAUSTED/u);
			assert.doesNotMatch(limited, /原文：/u);
			assert.deepEqual([requests, searches, fetches], [2, 1, 2]);
		}
		for (const [status, reason] of [
			["no_results", "未找到相关攻略"],
			["source_restricted", "没有获准"],
			["no_readable_body", "没有可读取的正文"],
			["metadata_incomplete", "目标角色、类型或适用版本声明"],
			["scope_mismatch", "不适用于本轮"],
			["inventory_mismatch", "不符合本轮已确认的库存或排除条件"],
		] as const) {
			const missing = await runQuestion(webVersion, async () => status);
			assert.ok(missing.includes(reason), missing);
			assert.doesNotMatch(missing, /模型不能替应用决定失败原因|原文：/u);
			assert.equal(requests, 3);
		}

		type GuideInput = Pick<typeof fixture, "title" | "sourceUrl" | "body">;
		const importFor = async (
			gameVersion: string,
			guide: GuideInput,
			conditions: string[] = [],
			entityId = "钟离",
		) => {
			await writeFile(
				file,
				JSON.stringify({
					...fixture,
					...guide,
					entityId,
					gameVersions: { from: gameVersion, through: gameVersion },
					applicability: conditions.length
						? {
								kind: "known",
								conditions,
								requiredCharacterIds: [],
								requiredEquipmentIds: [],
							}
						: { kind: "unrestricted" },
				}),
				"utf8",
			);
			const out = await cli("guides", "import", file, "--publish");
			const imported = /已发布攻略：([a-f0-9]{64})/u.exec(out)?.[1];
			assert.ok(imported, out);
			return imported;
		};
		const unusedVersion = async (start: number, entityId = "钟离") => {
			for (let minor = start; minor < 100; minor++) {
				const gameVersion = `984.${minor}`;
				const probe = await retrieval.searchGuides(
					{
						...scope,
						versionScope: { kind: "exact", version: gameVersion },
						entityId,
					},
					`${entityId}配队`,
					signal,
				);
				assert.notEqual(probe.kind, "error", JSON.stringify(probe));
				if (probe.kind === "miss") return gameVersion;
			}
			throw new Error("验收库没有空闲版本");
		};
		type Readback = {
			status: string;
			evidenceId: string;
			body: string;
			sourceUrl: string;
			documentVersionId: string;
			conditions: string[];
		};
		type ExpectedGuide = GuideInput & { versionId: string };
		const forGuides = (
			guides: readonly ExpectedGuide[],
			answer: (reads: readonly Readback[]) => object,
			entityId = "钟离",
		) => {
			const reads: Readback[] = [];
			let candidates: string[] = [];
			return (n: number, result: unknown) => {
				const tool = (name: string, args: object) => ({
					type: "function_call",
					id: `fc_${n}`,
					call_id: `call_${n}`,
					name,
					arguments: JSON.stringify(args),
				});
				if (n === 1)
					return tool("search_guides", { query: `${entityId} 配队` });
				if (n === 2) {
					const found = result as {
						status: string;
						candidates: { title: string; candidateId: string }[];
					};
					assert.equal(found.status, "hit");
					candidates = guides.map(({ title }) => {
						const candidate = found.candidates.find(
							(row) => row.title === title,
						);
						assert.ok(candidate, `未检索到「${title}」`);
						return candidate.candidateId;
					});
				} else {
					const read = result as Readback;
					const expected = guides[n - 3];
					assert.ok(expected);
					assert.equal(read.status, "complete");
					assert.equal(read.documentVersionId, expected.versionId);
					assert.equal(read.sourceUrl, expected.sourceUrl);
					assert.equal(
						read.body,
						expected.body.split("\n").slice(1).join("\n"),
					);
					reads.push(read);
				}
				const next = candidates[reads.length];
				if (next) return tool("read_guide", { candidateId: next });
				assert.equal(reads.length, guides.length);
				return {
					type: "message",
					id: `msg_${n}`,
					role: "assistant",
					content: [
						{ type: "output_text", text: JSON.stringify(answer(reads)) },
					],
				};
			};
		};
		const recommendations = (
			[read]: readonly Readback[],
			team: readonly {
				characterId: string;
				equipmentIds: readonly string[];
			}[] = sampleTeam,
		) => {
			assert.ok(read);
			return {
				recommendations: [
					{
						team,
						evidenceIds: [read.evidenceId],
					},
				],
				gaps: [],
			};
		};
		const ask = async (
			gameVersion: string,
			finish: RegExp,
			sessionId?: string,
		) =>
			playCli([
				[/命令：/u, sessionId ? `/open ${sessionId}` : "/new"],
				[/已创建会话|已打开会话/u, `钟离 ${gameVersion} 通用配队`],
				[finish, "/exit"],
			]);
		const sessionFrom = (transcript: string) => {
			const id = /已创建会话 ([a-f0-9-]{36})/u.exec(transcript)?.[1];
			assert.ok(id, transcript);
			return id;
		};
		await t.test(
			"V2-P05 · 补查后只给配队思路，重检引用及历史复用",
			async () => {
				const target = "行秋";
				const page = `${target}/${randomUUID()}/攻略`;
				const pageUrl = wikiUrl(page);
				const quote = "备选辅助：香菱。";
				const summary = "辅助位可考虑香菱，再根据其他位置调整队伍。";
				const wiki = [
					"== 阵容搭配 ==",
					`目标角色：${target}`,
					"=== 输出位置 ===",
					"只说明位置，不指定完整队伍。",
					"=== 辅助位置 ===",
					quote,
				].join("\n");
				const trace: string[] = [];
				const pull = webSupplement({
					...options,
					source: new BwikiSource(async (input, init) => {
						const params = wikiRequest(input, init);
						if (params.get("action") === "opensearch") {
							assert.equal(params.get("search"), target);
							trace.push("search");
							return Response.json([target, [page], [], [pageUrl]]);
						}
						const inspected = params.has("page");
						trace.push(inspected ? "inspect" : "read");
						return fakeWikiParse(page, wiki, inspected);
					}),
				});
				scripted = (n, value) => {
					const result = value as
						| {
								status: string;
								candidates?: { candidateId: string; preview: string }[];
								evidenceId?: string;
								body?: string;
								applicability?: string;
						  }
						| undefined;
					const tool = (name: string, args: object) => ({
						type: "function_call",
						id: `fc_${n}`,
						call_id: `call_${n}`,
						name,
						arguments: JSON.stringify(args),
					});
					if (!result || result.status === "indexed")
						return tool("search_guides", { query: `${target} 配队` });
					if (result.status === "miss")
						return tool("search_web_guides", { query: `${target} 配队` });
					if (result.status === "hit") {
						const candidate = result.candidates?.find(({ preview }) =>
							preview.includes(quote),
						);
						assert.ok(candidate, JSON.stringify(result));
						return tool("read_guide", { candidateId: candidate.candidateId });
					}
					assert.equal(result.status, "complete");
					assert.equal(result.applicability, "unknown");
					assert.ok(result.body?.includes(quote));
					return {
						type: "message",
						id: `msg_${n}`,
						role: "assistant",
						content: [
							{
								type: "output_text",
								text: JSON.stringify({
									recommendations: [],
									gaps: [],
									observations: [
										{
											kind: "teammate",
											text: summary,
											evidenceId: result.evidenceId,
											excerpt: quote,
										},
									],
								}),
							},
						],
					};
				};
				const warning = /该攻略版本未知，请注意时效性/u;
				const run = () =>
					playCli(
						[
							[/命令：/u, "/new"],
							[/已创建会话/u, `${target}最新通用配队`],
							[warning, "/exit"],
						],
						pull,
					);
				const labeled = (transcript: string) => {
					assert.match(transcript, /配队思路：\n队友选择：辅助位可考虑香菱/u);
					assert.equal(transcript.split(quote).length - 1, 1);
					assert.doesNotMatch(transcript, /资料不足：/u);
					assert.ok(transcript.includes(`原文：${pageUrl}`));
					assert.equal(
						transcript.match(/该攻略版本未知，请注意时效性/gu)?.length,
						1,
					);
					assert.doesNotMatch(transcript, /参考配队：|可用配队：/u);
				};
				try {
					const first = await run();
					labeled(first);
					assert.deepEqual(
						[requests, trace],
						[5, ["search", "inspect", "read"]],
					);
					labeled(await run());
					assert.deepEqual(
						[requests, trace],
						[3, ["search", "inspect", "read"]],
					);
					const history = await playCli(
						[
							[/命令：/u, `/open ${sessionFrom(first)}`],
							[/已打开会话/u, "/history"],
							[warning, "/exit"],
						],
						pull,
					);
					labeled(history);
					assert.equal(requests, 0);
					const rows = await pool.query(
						`SELECT status, game_from, applicability->>'kind' AS applicability
					 FROM guide_version JOIN guide_source ON source_id = guide_source.id WHERE source_url = $1`,
						[pageUrl],
					);
					assert.deepEqual(rows.rows, [
						{ status: "published", game_from: null, applicability: "unknown" },
					]);
				} finally {
					scripted = undefined;
				}
			},
		);
		await t.test(
			"AT-10 · 补查可发布与库存冲突的同范围攻略，不发送库存",
			async () => {
				for (const [target, allowed, blocked, order] of [
					["钟离", "西风长枪", "黑缨枪", ["blocked", "allowed"]],
					["香菱", "绝缘之旗印", "教官", ["allowed", "blocked"]],
					["香菱", "绝缘之旗印", "教官", ["blocked"]],
				] as const) {
					const gameVersion = await unusedVersion(70, target);
					const frozen = {
						...scope,
						entityId: target,
						versionScope: { kind: "exact" as const, version: gameVersion },
						teamMode: "inventory" as const,
						scopeId: "ordered-web-fixture",
						playerConstraints: {
							targetHolding: "owned" as const,
							teammates: {
								kind: "known" as const,
								items: sampleTeam
									.map((s) => s.characterId)
									.filter((id) => id !== target),
							},
							equipment: { kind: "known" as const, items: [allowed] },
							excludedEquipment: [blocked],
						},
					};
					const root = `${target}/${randomUUID()}/`;
					const titles = order.map((tag) => root + tag);
					const calls: string[] = [];
					const read: string[] = [];
					let searched = 0;
					const pull = webSupplement({
						...options,
						source: new BwikiSource(async (input, init) => {
							const params = wikiRequest(input, init);
							if (params.get("action") === "opensearch") {
								searched++;
								assert.equal(params.get("search"), target);
								return Response.json([target, titles, [], titles.map(wikiUrl)]);
							}
							const inspect = params.has("page");
							const offset = inspect
								? titles.indexOf(params.get("page") ?? "")
								: Number(params.get("oldid")) - revision.id;
							const candidate = titles[offset];
							assert.ok(candidate);
							(inspect ? calls : read).push(candidate);
							const gear = candidate.endsWith("/allowed") ? allowed : blocked;
							return fakeWikiParse(
								candidate,
								`== 阵容搭配 ==\n适用版本：${gameVersion}\n目标角色：${target}\n攻略类型：配队\n适用条件：持有装备 ${gear}\n\n阵容为钟离、班尼特、香菱、行秋；为${target}装备${gear}。`,
								inspect,
								revision.id + offset,
							);
						}),
					});
					const budget = new TurnBudget(
						{
							...readConfig({}).limits,
							fetchCalls: 3,
						},
						signal,
					);
					try {
						assert.equal(await pull(frozen, budget, signal), "indexed");
						assert.equal(searched, 1);
						const shouldRead = titles.slice(0, 1);
						assert.deepEqual(read, shouldRead);
						assert.deepEqual(calls, shouldRead);
						assert.equal(
							budget.summary().fetchCalls,
							calls.length + read.length,
						);
						const hit = await retrieval.searchGuides(
							frozen,
							`${target} 配队`,
							signal,
						);
						assert.ok(hit.kind === "hit");
						assert.equal(hit.candidates.length, 1);
						assert.equal(
							hit.candidates[0]?.chunk.sourceUrl,
							wikiUrl(titles[0] ?? ""),
						);
					} finally {
						budget.close();
					}
				}
			},
		);

		await t.test("AT-06 · 同一引用里的多套实际配装按本轮库存取舍", async () => {
			for (const [target, blocked, owned, restriction] of [
				["钟离", "黑缨枪", "西风长枪", "我没有"],
				["香菱", "教官", "绝缘之旗印", "本轮不用"],
			] as const) {
				const gameVersion = await unusedVersion(15, target);
				const lineup = [
					target,
					...sampleTeam
						.map((slot) => slot.characterId)
						.filter((id) => id !== target),
				];
				const guide = {
					title: `${target}隔离配队与两套装备选择`,
					sourceUrl: `https://example.invalid/actual-lineup/${randomUUID()}`,
					body: `## 两套方案\n四人阵容为${lineup.join("、")}。${target}可选择「${blocked}」或「${owned}」，只选一项。`,
				};
				const versionId = await importFor(gameVersion, guide, [], target);
				for (const chosen of [blocked, owned]) {
					scripted = forGuides(
						[{ ...guide, versionId }],
						(reads) =>
							recommendations(
								reads,
								lineup.map((characterId) => ({
									characterId,
									equipmentIds: characterId === target ? [chosen] : [],
								})),
							),
						target,
					);
					const transcript = await playCli([
						[/命令：/u, "/new"],
						[
							/已创建会话/u,
							`${target} ${gameVersion} 按库存配队；我有${lineup.join("、")}、${owned}；${restriction}${blocked}`,
						],
						[/原文：.*\/actual-lineup\//u, "/exit"],
					]);
					assert.equal(requests, 3);
					if (chosen === blocked) {
						assert.match(transcript, /参考配队：/u);
						assert.match(transcript, /(?:尚未持有|本轮排除)装备/u);
						assert.doesNotMatch(transcript, /可用配队：/u);
						assert.ok(transcript.includes(guide.sourceUrl));
					} else {
						const recommendation = transcript
							.split("\n")
							.find((line) => line.startsWith("可用配队："));
						assert.equal(
							recommendation,
							`可用配队：${target}（${owned}）＋${lineup.slice(1).join("＋")}`,
							transcript,
						);
						assert.ok(transcript.includes(guide.sourceUrl));
					}
				}
			}
		});

		await t.test("AT-05 · 同一来源修订后，CLI 历史仍可定位原版本", async () => {
			const gameVersion = await unusedVersion(1);
			const sourceUrl = `https://example.invalid/revisions/${randomUUID()}`;
			const previous = {
				title: "钟离配队修订前",
				sourceUrl,
				body: "## 初版方案\n推荐钟离、班尼特、香菱、行秋组队。",
			};
			const oldId = await importFor(gameVersion, previous);
			scripted = forGuides(
				[{ ...previous, versionId: oldId }],
				recommendations,
			);
			const original = await ask(gameVersion, /原文：.*\/revisions\//u);
			assert.equal(requests, 3);
			const sessionId = sessionFrom(original);
			const revised = {
				title: "钟离配队修订稿",
				sourceUrl,
				body: "## 修订方案\n推荐钟离、班尼特、香菱、砂糖组队。",
			};
			const newId = await importFor(gameVersion, revised);
			assert.notEqual(newId, oldId);
			scripted = undefined;
			const history = await playCli([
				[/命令：/u, `/open ${sessionId}`],
				[/已打开会话/u, "/history"],
				[/原文：.*\/revisions\//u, "/exit"],
			]);
			assert.equal(requests, 0);
			const old = await repository.forPublication(
				oldId,
				storage.embedding.id,
				signal,
			);
			assert.equal(old?.chunks.length, 1);
			const oldChunk = old?.chunks[0];
			assert.ok(oldChunk);
			const oldSnapshot = await repository.snapshot(
				{ ...filter, gameVersion },
				signal,
			);
			const archived = await repository.read(oldSnapshot, oldChunk.id, signal);
			assert.equal(archived?.chunk.documentVersionId, oldId);
			assert.equal(archived.chunk.sourceUrl, sourceUrl);
			assert.equal(archived.body, "推荐钟离、班尼特、香菱、行秋组队。");
			assert.ok(
				history.includes(
					`引用：${previous.title}｜${gameVersion}｜${archived.chunk.location}\n${archived.body}\n原文：${sourceUrl}`,
				),
				history,
			);
			assert.doesNotMatch(history, /推荐钟离、班尼特、香菱、砂糖组队/u);
			const log = await readFile(
				join(temp, "sessions", sessionId, "turns.jsonl"),
				"utf8",
			);
			assert.match(log, new RegExp(`"documentVersionId":"${oldId}"`, "u"));
			assert.ok(!log.includes(newId));
			scripted = forGuides([{ ...revised, versionId: newId }], (reads) =>
				recommendations(
					reads,
					sampleTeam.map((slot) =>
						slot.characterId === "行秋"
							? { ...slot, characterId: "砂糖" }
							: slot,
					),
				),
			);
			const newAnswer = await ask(
				gameVersion,
				/原文：.*\/revisions\//u,
				sessionId,
			);
			assert.equal(requests, 3);
			assert.match(newAnswer, /引用：钟离配队修订稿.*修订方案/u);
			assert.match(newAnswer, /参考配队：钟离＋班尼特＋香菱＋砂糖/u);
			assert.doesNotMatch(newAnswer, /推荐钟离、班尼特、香菱、行秋组队/u);
			const combined = await readFile(
				join(temp, "sessions", sessionId, "turns.jsonl"),
				"utf8",
			);
			assert.ok(combined.startsWith(log), "新轮只能追加，不能改写历史引用");
			assert.match(
				combined.slice(log.length),
				new RegExp(`"documentVersionId":"${newId}"`, "u"),
			);
		});

		await t.test("AT-06 · 同范围冲突展示双方原文，跨轮证据被拒", async () => {
			const gameVersion = await unusedVersion(50);
			const prerequisite = "同一隔离配队前提";
			const path = `https://example.invalid/dispute/${randomUUID()}`;
			const first = {
				title: "钟离配队冲突资料甲",
				sourceUrl: `${path}/a`,
				body: "## 资料甲结论\n在同一隔离配队前提下，钟离的配队必须使用示例装备甲。",
			};
			const second = {
				title: "钟离配队冲突资料乙",
				sourceUrl: `${path}/b`,
				body: "## 资料乙结论\n在同一隔离配队前提下，钟离的配队不需要示例装备甲。",
			};
			const guides = [
				{
					...first,
					versionId: await importFor(gameVersion, first, [prerequisite]),
				},
				{
					...second,
					versionId: await importFor(gameVersion, second, [prerequisite]),
				},
			];
			let staleEvidence: string | undefined;
			const opposing = (reads: readonly Readback[], useStale: boolean) => {
				assert.equal(reads.length, 2);
				if (!useStale) staleEvidence = reads[1]?.evidenceId;
				else {
					assert.ok(staleEvidence);
					assert.notEqual(reads[1]?.evidenceId, staleEvidence);
				}
				return {
					recommendations: [],
					gaps: [
						{
							kind: "conflict",
							sides: reads.map((read, index) => {
								assert.deepEqual(read.conditions, [prerequisite]);
								return {
									conclusion: read.body,
									conditions: read.conditions,
									evidenceIds: [
										useStale && index === 1 ? staleEvidence : read.evidenceId,
									],
								};
							}),
						},
					],
				};
			};
			scripted = forGuides(guides, (reads) => opposing(reads, false));
			const displayed = await ask(
				gameVersion,
				/原文：[^\n]+\/dispute\/[^\n]+\/b/u,
			);
			assert.equal(requests, 4);
			assert.match(displayed, /存在分歧，不能给出唯一结论/u);
			assert.match(displayed, /观点 1：[^\n]+必须使用示例装备甲/u);
			assert.match(displayed, /观点 2：[^\n]+不需要示例装备甲/u);
			assert.equal(displayed.match(/适用前提：同一隔离配队前提/gu)?.length, 2);
			assert.equal(displayed.match(/引用：/gu)?.length, 2);
			for (const guide of guides) {
				const [heading, excerpt] = guide.body.split("\n");
				assert.ok(heading && excerpt);
				assert.ok(
					displayed.includes(
						`引用：${guide.title}｜${gameVersion}｜${heading.slice(3)} · 第 2–2 行\n${excerpt}\n原文：${guide.sourceUrl}`,
					),
					displayed,
				);
			}
			scripted = forGuides(guides, (reads) => opposing(reads, true));
			const rejected = await ask(
				gameVersion,
				/INVALID_CITATION：/u,
				sessionFrom(displayed),
			);
			assert.equal(requests, 4);
			assert.match(rejected, /INVALID_CITATION/u);
			assert.doesNotMatch(rejected, /观点 [12]：|引用：|原文：/u);
		});

		await t.test(
			"V2-P04 · 未知版本发布、最新优先、指定版本确认与历史提示",
			async () => {
				scripted = undefined;
				wikitext = content.replace(/^适用游戏版本：[^\n]*\n/mu, "");
				const caution = /该攻略版本未知，请注意时效性/gu;
				const stopAfterCaution = /该攻略版本未知，请注意时效性/u;
				const latest = (
					input: string,
					currentVersion = "",
					finish = stopAfterCaution,
				) =>
					playCli(
						[
							[/命令：/u, "/new"],
							[/已创建会话/u, input],
							[finish, "/exit"],
						],
						supplement,
						8,
						6,
						currentVersion,
					);
				searches = 0;
				fetches = 0;
				const firstLatest = await latest("钟离最新通用配队");
				assert.match(firstLatest, /｜版本未知｜/u);
				assert.ok(firstLatest.includes(`原文：${url}`), firstLatest);
				assert.equal(firstLatest.match(caution)?.length, 1);
				assert.deepEqual([requests, searches, fetches], [5, 1, 2]);
				const nullable = await pool.query(
					`SELECT game_from, game_through, game_from_key, game_through_key,
					web_evidence->>'unknownGameVersionReason' AS reason
				 FROM guide_version JOIN guide_source ON source_id = guide_source.id
				 WHERE source_url = $1 AND game_from IS NULL AND status = 'published'`,
					[url],
				);
				assert.deepEqual(nullable.rows, [
					{
						game_from: null,
						game_through: null,
						game_from_key: null,
						game_through_key: null,
						reason: "not_stated",
					},
				]);
				await assert.rejects(
					pool.query(
						"UPDATE guide_version SET game_from = '7.1' WHERE game_from IS NULL",
					),
					{ code: "23514" },
				);

				const unversioned = await latest("钟离怎么配队");
				assert.equal(unversioned.match(caution)?.length, 1);
				assert.deepEqual([requests, searches, fetches], [3, 1, 2]);
				const preferred = await latest(
					"钟离最新通用配队",
					filter.gameVersion,
					/原文：/u,
				);
				assert.match(preferred, /原文：https:\/\/example\.invalid\//u);
				assert.doesNotMatch(preferred, /版本未知|注意时效性/u);
				assert.deepEqual([requests, searches, fetches], [3, 1, 2]);

				const absentVersion = await unusedVersion(95);
				const question = await runQuestion(
					absentVersion,
					async () => "no_results",
				);
				assert.match(question, /是否查看版本未知的参考/u);
				assert.doesNotMatch(
					question,
					/参考配队：|引用：|原文：|该攻略版本未知/u,
				);
				const resume = (id: string, option: string, finish: RegExp) =>
					playCli(
						[
							[/命令：/u, `/open ${id}`],
							[/是否查看版本未知的参考/u, option],
							[finish, "/exit"],
						],
						async () => assert.fail("确认参考不能重新补查"),
					);
				const accepted = await resume(
					sessionFrom(question),
					"接受",
					stopAfterCaution,
				);
				assert.match(accepted, /｜版本未知｜/u);
				assert.equal(accepted.match(caution)?.length, 1);
				assert.equal(requests, 3);
				const reopened = await playCli([
					[/命令：/u, `/open ${sessionFrom(question)}`],
					[/已打开会话/u, "/history"],
					[stopAfterCaution, "/exit"],
				]);
				assert.equal(reopened.match(caution)?.length, 1);
				assert.equal(requests, 0);
				assert.deepEqual([searches, fetches], [1, 2]);

				const declineQuestion = await runQuestion(
					absentVersion,
					async () => "source_restricted",
				);
				const declined = await resume(
					sessionFrom(declineQuestion),
					"拒绝",
					/未使用版本未知的参考/u,
				);
				assert.doesNotMatch(declined, /参考配队：|原文：|该攻略版本未知/u);
				assert.equal(requests, 0);
			},
		);
	} finally {
		await pool.end();
		await rm(temp, { recursive: true, force: true });
		if (stub)
			await new Promise<void>((resolve, reject) =>
				stub.close((error) => (error ? reject(error) : resolve())),
			);
	}
});
