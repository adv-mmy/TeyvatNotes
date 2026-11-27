import assert from "node:assert/strict";
import { test } from "node:test";
import type {
	GuideChunkMetadata,
	GuideData,
} from "../../src/application/ports/guide-data.js";
import {
	defaultRetrievalLimits,
	GuideRetrieval,
} from "../../src/application/retrieve-guides.js";
import {
	type GuideEvidence,
	type GuideEvidenceView,
	validateGuideAnswer as validateAnswer,
} from "../../src/domain/answer-validation.js";
import { rankGuides } from "../../src/domain/guide-ranking.js";
import {
	type FrozenQueryScope,
	type TerminologyEntry,
	versionApplies,
} from "../../src/domain/query-scope.js";

const scope: FrozenQueryScope = {
	scopeId: "s1",
	entityId: "甲",
	gameVersion: "7.10",
	goal: "配队",
	guideKind: "team",
	teamMode: "general",
	playerConstraints: {
		targetHolding: "unknown",
		teammates: { kind: "unknown" },
		equipment: { kind: "unknown" },
	},
};

function pagedRetrieval(
	expanding: "keyword" | "vector",
	failOnSecondPage = false,
) {
	const chunks = new Map<string, GuideChunkMetadata>(
		["first", "second"].map((id) => [
			id,
			{
				id,
				documentVersionId: id,
				entityId: scope.entityId,
				guideKind: "team",
				gameVersions: { from: scope.gameVersion, through: scope.gameVersion },
				applicability: { kind: "unrestricted" },
				published: true,
				indexReady: true,
				title: id,
				sourceUrl: "https://example.test/guide",
				location: "第 1 段",
				preview: id,
			},
		]),
	);
	const keywordCursors: (string | null)[] = [];
	const vectorLimits: number[] = [];
	const data: GuideData = {
		repository: {
			async snapshot() {
				return { revision: "r1", embeddingId: "isolated" };
			},
			async keywords(_filter, _snapshot, _query, cursor) {
				keywordCursors.push(cursor);
				if (cursor === "p2") {
					if (failOnSecondPage) throw new Error("分页依赖失败");
					return { ids: ["first", "second"], next: null };
				}
				return { ids: ["first"], next: expanding === "keyword" ? "p2" : null };
			},
			async eligibleIds() {
				return { ids: [...chunks.keys()], next: null };
			},
			async metadata(_snapshot, ids) {
				return ids.flatMap((id) => {
					const entry = chunks.get(id);
					return entry ? [entry] : [];
				});
			},
			async read() {
				assert.fail("召回阶段不能读取正文");
			},
			async assertSnapshot() {},
		},
		embeddings: {
			async embed() {
				return [1, 0];
			},
		},
		vectorIndex: {
			async hasAll(_config, ids) {
				return ids.every((id) => chunks.has(id));
			},
			async nearest(_id, _vector, limit) {
				vectorLimits.push(limit);
				return [
					{ id: "first", score: 0.9 },
					...(expanding === "vector" && limit > 2
						? [{ id: "second", score: 0.8 }]
						: []),
				];
			},
			async vectors(_config, ids) {
				return new Map(ids.map((id) => [id, [1, 0]]));
			},
		},
	};
	return {
		retrieval: new GuideRetrieval(data, {
			...defaultRetrievalLimits,
			pageSize: 2,
			maxPages: 2,
			maxScanned: 4,
			maxCandidates: 2,
		}),
		keywordCursors,
		vectorLimits,
	};
}

for (const lane of ["keyword", "vector"] as const) {
	test(`${lane} 首次已有合格候选但未达到目标数时继续检索`, async () => {
		const { retrieval, keywordCursors, vectorLimits } = pagedRetrieval(lane);
		const result = await retrieval.searchGuides(
			scope,
			"配队",
			new AbortController().signal,
		);
		assert.equal(result.kind, "hit");
		if (result.kind !== "hit") return;
		assert.deepEqual(
			result.candidates.map(({ chunk }) => chunk.id),
			["first", "second"],
		);
		assert.deepEqual(
			keywordCursors,
			lane === "keyword" ? [null, "p2"] : [null],
		);
		assert.deepEqual(vectorLimits, [2, 4]);
	});
}

test("已部分命中时后续关键词页故障，仍报告依赖故障而非命中或未命中", async () => {
	const { retrieval } = pagedRetrieval("keyword", true);
	assert.deepEqual(
		await retrieval.searchGuides(scope, "配队", new AbortController().signal),
		{
			kind: "error",
			code: "RETRIEVAL_UNAVAILABLE",
		},
	);
});

const reference: GuideEvidence = {
	evidenceId: "e1",
	turnId: "t1",
	scopeId: "s1",
	entityId: "甲",
	guideKind: "team",
	gameVersions: { from: "7.2", through: "7.10" },
	windowComplete: true,
	chunkId: "c1",
	requiredCharacterIds: ["甲", "乙"],
	requiredEquipmentIds: ["试验弓"],
	conditions: ["须持有试验弓"],
	citation: {
		source: "原文",
		url: "https://example.test/a",
		version: "7.10",
		documentVersionId: "d1",
		location: "第 2 段",
		excerpt: "完整样本",
	},
};
const testView = (...records: GuideEvidence[]): GuideEvidenceView => ({
	evidence: new Map(records.map((record) => [record.evidenceId, record])),
	gap: true,
	readAllCandidates: true,
});
const terms: readonly TerminologyEntry[] = [
	...["甲", "乙", "丙", "丁", "戊"].map((name) => ({
		category: "character" as const,
		name,
		aliases: [`角色${name}`],
	})),
	{ category: "weapon", name: "试验弓", aliases: ["小弓"] },
	{ category: "weapon", name: "替代弓", aliases: [] },
	{ category: "artifact", name: "试验套", aliases: ["小套"] },
];
const validateGuideAnswer = (
	raw: string,
	scope: FrozenQueryScope,
	turnId: string,
	view: GuideEvidenceView,
) => validateAnswer(raw, scope, turnId, view, terms);
const recommendation = {
	team: ["甲", "乙", "丙", "丁"].map((characterId) => ({
		characterId,
		equipmentIds: [] as string[],
	})),
	evidenceIds: ["e1"],
};
const draft = (items: unknown[], gaps: unknown[] = []) =>
	JSON.stringify({ recommendations: items, gaps });

test("RRF 跨通道合并，同一路重复不叠加，平分按稳定 ID", () => {
	assert.deepEqual(
		rankGuides([
			["B", "B", "A"],
			["A", "D"],
		]).map(({ id }) => id),
		["A", "B", "D"],
	);
	assert.deepEqual(
		rankGuides([["B"], ["A"]]).map(({ id }) => id),
		["A", "B"],
	);
	assert.equal(versionApplies("7.10", { from: "7.2", through: "7.10" }), true);
	assert.equal(versionApplies("7.2", { from: "7.10", through: "7.11" }), false);
});

test("通用建议保留明确前提，引用从证据登记簿生成", () => {
	const result = validateGuideAnswer(
		draft([recommendation]),
		scope,
		"t1",
		testView(reference),
	);
	assert.equal(result.status, "answered");
	if (result.status === "answered") {
		assert.match(result.text, /参考配队：甲＋乙＋丙＋丁/u);
		assert.match(result.text, /须持有试验弓/u);
		assert.deepEqual(result.citations, [reference.citation]);
	}
	assert.deepEqual(
		validateGuideAnswer(
			draft([{ ...recommendation, text: "伪装为其他阵容" }]),
			scope,
			"t1",
			testView(reference),
		),
		{
			status: "failed",
			code: "INVALID_ANSWER_STRUCTURE",
			validationDetail: "unexpected_fields:$.recommendations[0]",
		},
	);
});

test("攻略格式错误只报告固定原因和字段路径，合法资料不足仍可展示", () => {
	const gap = { kind: "insufficient_evidence", text: "没有适用资料" };
	for (const [raw, detail] of [
		["未公开的非 JSON 正文", "json_invalid"],
		[`\`\`\`json\n${draft([], [gap])}\n\`\`\``, "json_code_fence"],
		["[]", "root_not_object"],
		[
			JSON.stringify({ recommendations: [recommendation] }),
			"missing_field:$.gaps",
		],
		[JSON.stringify({ recommendations: [], gaps: {} }), "invalid_field:$.gaps"],
		[draft([], []), "status_conflict:both_empty"],
		[draft([recommendation], [gap]), "status_conflict:both_nonempty"],
		[
			draft([{ evidenceIds: ["e1"] }]),
			"missing_field:$.recommendations[0].team",
		],
		[
			JSON.stringify({
				recommendations: [],
				gaps: [gap],
				不应记录的属性名: "不应记录的属性值",
			}),
			"unexpected_fields:$",
		],
		[
			draft([], [{ kind: "不应记录的类型名", text: "不应记录的正文" }]),
			"unsupported_gap_kind",
		],
	] as const)
		assert.deepEqual(validateGuideAnswer(raw, scope, "t1", testView()), {
			status: "failed",
			code: "INVALID_ANSWER_STRUCTURE",
			validationDetail: detail,
		});
	assert.equal(
		validateGuideAnswer(draft([], [gap]), scope, "t1", testView()).status,
		"insufficient_evidence",
	);
	assert.deepEqual(
		validateGuideAnswer(
			draft([{ ...recommendation, evidenceIds: undefined }]),
			scope,
			"t1",
			testView(reference),
		),
		{ status: "failed", code: "INVALID_CITATION" },
	);
});

test("伪造、跨轮、错范围、错版本、正文截断都不是引用", () => {
	for (const record of [
		{ ...reference, evidenceId: "other" },
		{ ...reference, turnId: "t0" },
		{ ...reference, scopeId: "s0" },
		{ ...reference, gameVersions: { from: "8.1", through: "8.1" } },
		{ ...reference, windowComplete: false },
	]) {
		assert.deepEqual(
			validateGuideAnswer(
				draft([recommendation]),
				scope,
				"t1",
				testView(record),
			),
			{
				status: "failed",
				code: "INVALID_CITATION",
			},
		);
	}
});

test("有效引用不能替代实际阵容的库存校验；标准名、别名及设备类别共用同一规则", () => {
	const source = {
		...reference,
		conditions: [],
		requiredCharacterIds: [],
		requiredEquipmentIds: [],
	};
	for (const [target, owned, excluded] of [
		["甲", ["试验弓"], "试验套"],
		["乙", ["试验套"], "替代弓"],
	] as const) {
		const inventory: FrozenQueryScope = {
			...scope,
			entityId: target,
			teamMode: "inventory",
			playerConstraints: {
				targetHolding: "owned",
				teammates: {
					kind: "known",
					items: ["甲", "乙", "丙", "丁"].filter((id) => id !== target),
				},
				equipment: { kind: "known", items: owned },
				excludedEquipment: [excluded],
			},
		};
		const read = testView({ ...source, entityId: target });
		const submit = (item: unknown, selected = inventory) =>
			validateGuideAnswer(draft([item]), selected, "t1", read);
		const withGear = (equipmentIds: string[]) => ({
			...recommendation,
			team: recommendation.team.map((slot) =>
				slot.characterId === target
					? { characterId: `角色${target}`, equipmentIds }
					: slot,
			),
		});
		const ok = submit(withGear([target === "甲" ? "小弓" : "小套"]));
		assert.equal(ok.status, "answered");
		if (ok.status === "answered")
			assert.ok(ok.text.includes(`${target}（${owned[0]}）`));
		for (const item of [
			withGear([excluded]),
			withGear([target === "甲" ? "替代弓" : "试验弓"]),
			{
				...recommendation,
				team: recommendation.team.map((slot) =>
					slot.characterId === "丁" ? { ...slot, characterId: "戊" } : slot,
				),
			},
		])
			assert.deepEqual(submit(item), {
				status: "failed",
				code: "INVALID_ANSWER_CONDITIONS",
			});
		assert.equal(
			submit(recommendation, {
				...inventory,
				playerConstraints: {
					...inventory.playerConstraints,
					equipment: { kind: "unknown" },
				},
			}).status,
			"answered",
		);
		for (const malformed of [
			{ ...recommendation, team: recommendation.team.slice(0, 3) },
			{
				...recommendation,
				team: recommendation.team.map((slot) => ({
					...slot,
					characterId: `角色${target}`,
				})),
			},
			withGear(["试验弓", "替代弓"]),
			withGear(["角色丙"]),
		]) {
			const rejected = submit(malformed);
			assert.equal(rejected.status, "failed");
			if (rejected.status === "failed")
				assert.equal(rejected.code, "INVALID_ANSWER_STRUCTURE");
		}
	}
});

test("按库存先用满足条件的替代；找不到且确有依据才追问未知装备", () => {
	const inventory: FrozenQueryScope = {
		...scope,
		teamMode: "inventory",
		playerConstraints: {
			targetHolding: "owned",
			teammates: { kind: "known", items: ["乙", "丙", "丁"] },
			equipment: { kind: "unknown" },
		},
	};
	const alternative = {
		...reference,
		evidenceId: "e2",
		chunkId: "c2",
		requiredEquipmentIds: [],
		conditions: [],
	};
	const question = draft(
		[],
		[
			{
				kind: "equipment_required",
				equipmentId: "试验弓",
				evidenceIds: ["e1"],
			},
		],
	);
	assert.equal(
		validateGuideAnswer(question, inventory, "t1", testView(reference)).status,
		"needs_clarification",
	);
	assert.equal(
		validateGuideAnswer(question, inventory, "t1", {
			...testView(reference),
			readAllCandidates: false,
		}).status,
		"failed",
	);
	assert.equal(
		validateGuideAnswer(
			question,
			inventory,
			"t1",
			testView(reference, alternative),
		).status,
		"failed",
	);
	const noEquipment: FrozenQueryScope = {
		...inventory,
		playerConstraints: {
			...inventory.playerConstraints,
			equipment: { kind: "known", items: [] },
		},
	};
	assert.equal(
		validateGuideAnswer(question, noEquipment, "t1", testView(reference))
			.status,
		"failed",
	);
	assert.equal(
		validateGuideAnswer(
			draft([recommendation]),
			inventory,
			"t1",
			testView(reference),
		).status,
		"failed",
	);
});

test("同范围冲突要分别引用双方，不用同一段原文冒充两个观点", () => {
	const second = { ...reference, evidenceId: "e2", chunkId: "c2" };
	const sides = [
		{
			conclusion: "配队通过。",
			conditions: reference.conditions,
			evidenceIds: ["e1"],
		},
		{
			conclusion: "不采用该配队。",
			conditions: reference.conditions,
			evidenceIds: ["e2"],
		},
	];
	const input = draft([], [{ kind: "conflict", sides }]);
	const result = validateGuideAnswer(
		input,
		scope,
		"t1",
		testView(reference, second),
	);
	assert.equal(result.status, "answered");
	if (result.status === "answered") {
		assert.match(result.text, /不能给出唯一结论/u);
		assert.equal(result.citations.length, 2);
	}
	assert.deepEqual(
		validateGuideAnswer(
			input,
			scope,
			"t1",
			testView(reference, { ...second, chunkId: reference.chunkId }),
		),
		{
			status: "failed",
			code: "INVALID_CITATION",
		},
	);
});
