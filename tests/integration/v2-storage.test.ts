import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
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
import { DeepSeekSource } from "../../src/adapters/web/deepseek-source.js";
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
	{ category: "weapon", name: "黑缨枪", aliases: ["史莱姆枪"] },
] as const;
const fixture = {
	title: "钟离配队验证",
	sourceUrl: "https://example.invalid/mock-team",
	entityId: "岩王帝君",
	guideKind: "team",
	gameVersions: { from: "7.2", through: "7.10" },
	applicability: { kind: "unrestricted" },
	body: "## 配队\n这是一段隔离验收正文，示例角色带黑缨枪。\n\n| 前提 | 示例 |\n| --- | --- |\n| 验收 | 虚构 |\n\n## 注意\n这段正文用于检验引用定位。",
};

test("DeepSeek 接口只检索链接；重定向能力未经验证时不发正文请求", async (t) => {
	const permit = {
		origin: "https://example.invalid",
		path: "/guides/",
		reference: "隔离",
		access: "fetch_store_cite",
	} as const;
	for (const url of [
		"https://127.0.0.1/guides/a",
		"http://example.invalid/guides/a",
		"https://example.invalid/outside",
		"https://u@example.invalid/guides/a",
		"https://example.invalid/guides/%2e%2e/u",
		"https://example.invalid/guides/a?next=elsewhere",
	]) {
		assert.equal(permittedUrl(url, permit), undefined);
	}
	let calls = 0;
	const url = `${permit.origin}/guides/1`;
	t.mock.method(
		globalThis,
		"fetch",
		async (_url: string, init: RequestInit) => {
			calls++;
			assert.equal(_url, "https://api.deepseek.com/v1/tools/search");
			assert.deepEqual(JSON.parse(String(init.body)), {
				query: "钟离 7.1 配队 site:example.invalid",
				count: 10,
				need_content: false,
			});
			return Response.json({
				results: [{ url, content: "意外正文", snippet: "摘要" }],
			});
		},
	);
	const api = new DeepSeekSource("isolated-only");
	const signal = new AbortController().signal;
	assert.deepEqual(
		await api.search("钟离 7.1 配队 site:example.invalid", signal),
		[url],
	);
	await assert.rejects(api.fetch(url, permit, signal), {
		message: "SOURCE_RESTRICTED",
	});
	assert.equal(calls, 1);
});

test("网页元数据保留依据，只索引建议；不把相互矛盾的声明、未知前提或占位内容当成攻略", () => {
	const title = "钟离配队";
	const header =
		"# 钟离配队\n适用版本：7.1\n目标角色：钟离\n攻略类型：配队\n适用条件：持有装备 史莱姆枪";
	const content = `${header}\n\n## 配队建议\n钟离可与虚构队友配队。`;
	const parse = (content: string) =>
		parseWebGuide(
			{ title, content },
			fixture.sourceUrl,
			"测试许可",
			terminology,
		);
	const parsed = parse(content);
	assert.equal(parsed.kind, "ready");
	if (parsed.kind !== "ready") return;
	assert.deepEqual(
		parsed.document.chunks.map((c) => c.body),
		["钟离可与虚构队友配队。"],
	);
	assert.equal(parsed.document.content, content);
	assert.equal(
		parsed.document.webEvidence?.fields.versions?.excerpt,
		"适用版本：7.1",
	);
	assert.equal(
		parsed.document.webEvidence?.fields.conditions?.location,
		"钟离配队 · 第 5 行",
	);
	assert.deepEqual(parsed.document.applicability, {
		kind: "known",
		conditions: ["持有装备 史莱姆枪"],
		requiredCharacterIds: [],
		requiredEquipmentIds: ["黑缨枪"],
	});
	for (const invalid of [
		content.replace("适用版本：7.1", "适用版本：7.1\n适用版本：7.2"),
		content.replace("持有装备 史莱姆枪", "未知\n必需装备：黑缨枪"),
		content.replace("钟离可与虚构队友配队。", "| --- | --- |"),
		content.replace("# 钟离配队", "# 未知角色配队"),
		content.replace("目标角色：钟离", "<!--\n目标角色：钟离\n-->"),
	])
		assert.equal(parse(invalid).kind, "incomplete");
});

test("导入规范化别名和版本，保留章节、表格及原文行号", () => {
	const parsed = prepareGuide(JSON.stringify(fixture), "json", terminology);
	assert.equal(parsed.entityId, "钟离");
	assert.equal(parsed.chunks.length, 2);
	assert.equal(parsed.chunks[0]?.location, "配队 · 第 2–6 行");
	assert.match(parsed.chunks[0]?.body ?? "", /\| 验收 \| 虚构 \|/u);
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
		...filter,
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
		const retrieval = new GuideRetrieval({
			repository,
			embeddings,
			vectorIndex,
		});
		assert.deepEqual(
			await retrieval.searchGuides(
				{ ...scope, gameVersion: "991.1" },
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
			assert.match(read.window.body, /隔离验收正文|引用定位/u);
		}
		assert.equal(
			(
				await retrieval.searchGuides(
					{ ...scope, gameVersion: "991.1" },
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
				const tool =
					!content || content.status === "indexed"
						? "search_guides"
						: content.status === "miss"
							? "search_web_guides"
							: content.status === "hit"
								? "read_guide"
								: undefined;
				const item = tool
					? {
							type: "function_call",
							id: `fc_${requests}`,
							call_id: `call_${requests}`,
							name: tool,
							arguments: JSON.stringify(
								tool !== "read_guide"
									? { query: "钟离配队" }
									: { candidateId: content.candidates[0].candidateId },
							),
						}
					: {
							type: "message",
							id: "msg_3",
							role: "assistant",
							content: [
								{
									type: "output_text",
									text: JSON.stringify({
										recommendations: [
											{
												text: content.body.includes("钟离可与虚构队友配队")
													? "钟离可与虚构队友配队。"
													: "隔离配队建议",
												conditions: [],
												requiredEntityIds: [],
												evidenceIds: [content.evidenceId],
											},
										],
										gaps: [],
									}),
								},
							],
						};
				return new Response(
					[
						{ type: "response.output_item.done", output_index: 0, item },
						{
							type: "response.completed",
							response: {
								id: `resp_${requests}`,
								status: "completed",
								output: [item],
							},
						},
					]
						.map((event) => `data: ${JSON.stringify(event)}\n\n`)
						.join(""),
					{ headers: { "content-type": "text/event-stream" } },
				);
			},
		);
		const secret = join(temp, "model-key");
		await writeFile(secret, "isolated-test-key", { mode: 0o600 });
		const runQuestion = async (
			gameVersion: string,
			supplement?: WebSupplement,
			toolLimit = 8,
		) => {
			requests = 0;
			const stdin = new PassThrough();
			const stdout = new PassThrough();
			let transcript = "";
			stdout.on("data", (data) => {
				const line = data.toString();
				transcript += line;
				if (line.includes("命令：")) setImmediate(() => stdin.write("/new\n"));
				if (line.includes("已创建会话"))
					setImmediate(() => stdin.write(`钟离 ${gameVersion} 通用配队\n`));
				if (/原文：|[A-Z_]{4,}：|需要补充条件：|资料不足：/u.test(line))
					setImmediate(() => stdin.end("/exit\n"));
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
			return transcript;
		};
		const transcript = await runQuestion(filter.gameVersion);
		assert.match(transcript, /隔离配队建议/u);
		assert.match(transcript, /原文：https:\/\/example\.invalid\//u);
		assert.equal(requests, 3);
		const permit = {
			origin: "https://example.invalid",
			path: "/allowed/",
			reference: "隔离夹具许可",
			access: "fetch_store_cite",
		} as const;
		const url = permit.origin + permit.path + randomUUID();
		// 复用隔离卷时选一个尚无样本的范围，不清表，也不靠随机数避开旧命中。
		const freeRange = await pool.query<{ minor: number }>(`
			SELECT minor FROM generate_series(0,999) AS minor
			WHERE NOT EXISTS (SELECT 1 FROM guide_version WHERE game_from_key <= 978000002 + minor * 1000
				AND game_through_key >= 978000001 + minor * 1000)
			ORDER BY minor LIMIT 1`);
		assert.ok(freeRange.rows[0]);
		let webVersion = `978.${freeRange.rows[0].minor}.1`;
		const webScope = { ...filter, gameVersion: webVersion };
		const content =
			"# 钟离配队\n适用游戏版本：" +
			webVersion +
			"\n目标角色：钟离\n攻略类型：配队\n适用条件：无额外限制\n\n## 配队建议\n钟离可与虚构队友配队。";
		let markdown = content;
		let searches = 0;
		let fetches = 0;
		const options = {
			source: {
				async search(query: string) {
					searches++;
					assert.equal(query, `钟离 ${webVersion} 配队 site:example.invalid`);
					return ["https://other.invalid/disallowed", url, url];
				},
				canFetch: () => true,
				async fetch(candidate: string) {
					fetches++;
					assert.equal(candidate, url);
					return { title: "钟离配队", content: markdown };
				},
			},
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
				return await call(webScope, budget, budget.signal);
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
		assert.deepEqual([searches, fetches], [1, 0]);
		assert.equal(
			await check(
				webSupplement({
					...options,
					source: {
						...options.source,
						async search() {
							return [];
						},
					},
				}),
			),
			"no_results",
		);
		assert.equal(
			await check(
				webSupplement({
					...options,
					source: { ...options.source, canFetch: () => false },
				}),
			),
			"source_restricted",
		);
		assert.equal(fetches, 0);
		for (const op of ["search", "fetch"] as const) {
			const budget = new TurnBudget(
				{ ...readConfig({}).limits, searchMs: 50, fetchMs: 50 },
				signal,
			);
			let observed: AbortSignal | undefined;
			const hang = (...args: unknown[]): Promise<never> => {
				observed = args.at(-1) as AbortSignal;
				return new Promise(() => {});
			};
			try {
				await assert.rejects(
					webSupplement({
						...options,
						source: { ...options.source, [op]: hang },
					})(webScope, budget, budget.signal),
					{ message: "TOOL_TIMEOUT" },
				);
				assert.equal(observed?.aborted, true);
				assert.equal(budget.summary().searchCalls, 1);
				assert.equal(budget.summary().fetchCalls, Number(op === "fetch"));
			} finally {
				budget.close();
			}
		}
		markdown = "请先登录";
		assert.equal(await check(supplement), "no_readable_body");
		markdown = content.replace(webVersion, "1.0");
		assert.equal(await check(supplement), "scope_mismatch");
		markdown = content.replace(`适用游戏版本：${webVersion}`, "");
		assert.equal(await check(supplement), "metadata_incomplete");
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
		markdown = content;
		const notVisible: VectorWritePort = {
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
		assert.equal(
			first.match(/钟离可与虚构队友配队。/gu)?.length,
			2,
			"结论与原文摘录均包含同一条建议",
		);
		assert.equal(first.match(/正在执行工具/gu)?.length, 4);
		assert.deepEqual([requests, searches, fetches], [5, 1, 1]);
		const second = await runQuestion(webVersion, supplement);
		assert.ok(second.includes(`原文：${url}`), second);
		assert.equal(second.match(/正在执行工具/gu)?.length, 2);
		assert.deepEqual([requests, searches, fetches], [3, 1, 1]);
		const nextVersion = webVersion.replace(/\.1$/u, ".2");
		markdown = content.replace(webVersion, nextVersion);
		webVersion = nextVersion;
		const limited = await runQuestion(webVersion, supplement, 2);
		assert.match(limited, /BUDGET_EXHAUSTED/u);
		assert.doesNotMatch(limited, /原文：/u);
		assert.deepEqual([requests, searches, fetches], [2, 2, 2]);
	} finally {
		await pool.end();
		await rm(temp, { recursive: true, force: true });
		if (stub)
			await new Promise<void>((resolve, reject) =>
				stub.close((error) => (error ? reject(error) : resolve())),
			);
	}
});
