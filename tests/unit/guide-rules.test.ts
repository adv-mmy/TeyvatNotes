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
	validateGuideAnswer,
} from "../../src/domain/answer-validation.js";
import { rankGuides } from "../../src/domain/guide-ranking.js";
import {
	type FrozenQueryScope,
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
const recommendation = {
	text: "建议甲与乙组队。",
	conditions: ["须持有试验弓"],
	requiredEntityIds: ["甲", "乙", "试验弓"],
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
		assert.match(result.text, /适用前提：须持有试验弓/u);
		assert.deepEqual(result.citations, [reference.citation]);
	}
	assert.deepEqual(
		validateGuideAnswer(
			draft([{ ...recommendation, conditions: [] }]),
			scope,
			"t1",
			testView(reference),
		),
		{
			status: "failed",
			code: "INVALID_ANSWER_CONDITIONS",
		},
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

test("按库存先用满足条件的替代；找不到且确有依据才追问未知装备", () => {
	const inventory: FrozenQueryScope = {
		...scope,
		teamMode: "inventory",
		playerConstraints: {
			targetHolding: "owned",
			teammates: { kind: "known", items: ["乙"] },
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
