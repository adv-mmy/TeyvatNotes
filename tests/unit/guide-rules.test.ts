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
	type GuideConditionDetail,
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
	versionScope: { kind: "exact", version: "7.10" },
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
				gameVersions: { from: "7.10", through: "7.10" },
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
	const versionLayers: (string | null)[] = [];
	const data: GuideData = {
		repository: {
			async snapshot(filter) {
				versionLayers.push(filter.gameVersion);
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
		versionLayers,
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

test("当前版本检索故障不进入未知版本层", async () => {
	const { retrieval, versionLayers } = pagedRetrieval("keyword", true);
	assert.deepEqual(
		await retrieval.searchGuides(
			{ ...scope, versionScope: { kind: "latest", currentVersion: "7.10" } },
			"配队",
			new AbortController().signal,
		),
		{ kind: "error", code: "RETRIEVAL_UNAVAILABLE" },
	);
	assert.deepEqual(versionLayers, ["7.10"]);
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
		excerpt: "示例阵容：甲＋乙＋丙＋丁。\n辅助位：丁。",
	},
};
const testView = (...records: GuideEvidence[]): GuideEvidenceView => ({
	evidence: new Map(records.map((record) => [record.evidenceId, record])),
	gap: true,
	hasUnreadCandidates: false,
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

test("只提供分工资料也能回答；可附原文未作为整队列出的综合配队", () => {
	const source: GuideEvidence = {
		...reference,
		fromWeb: true,
		applicabilityUnknown: true,
		conditions: [],
		requiredCharacterIds: [],
		requiredEquipmentIds: [],
		citation: {
			...reference.citation,
			excerpt: "队友候选：乙、丙。\n辅助位：丁可减抗。",
		},
	};
	const observation = {
		kind: "role",
		text: "辅助位可选丁，主要职责是降低敌方抗性。",
		evidenceId: "e1",
		excerpt: "辅助位：丁可减抗。",
	};
	const resolve = (item: object, teams: unknown[] = [], read = source) =>
		validateGuideAnswer(
			JSON.stringify({
				observations: [item],
				recommendations: teams,
				gaps: [],
			}),
			scope,
			"t1",
			testView(read),
		);

	const summary = resolve(observation);
	assert.ok(summary.status === "answered");
	assert.match(summary.text, /^配队思路：\n位置分工：辅助位可选丁/u);
	assert.match(summary.text, /适用条件未确认/u);
	assert.doesNotMatch(summary.text, /辅助位：丁可减抗。|资料不足|参考配队/u);
	assert.deepEqual(summary.citations, [
		{ ...source.citation, excerpt: observation.excerpt },
	]);

	const withTeam = resolve(observation, [recommendation]);
	assert.ok(withTeam.status === "answered");
	assert.match(
		withTeam.text,
		/^配队思路：[\s\S]+配队建议（模型整理）：\n参考配队：甲＋乙＋丙＋丁/u,
	);
	assert.deepEqual(withTeam.citations, summary.citations);
	assert.deepEqual(
		resolve(observation, [{ ...recommendation, evidenceIds: ["forged"] }]),
		{ status: "failed", code: "INVALID_CITATION" },
	);
	for (const invalid of [
		resolve({ ...observation, excerpt: "辅助位：丙可减抗。" }),
		resolve({ ...observation, evidenceId: "not-authorized" }),
		resolve(observation, [], { ...source, turnId: "old" }),
		resolve(observation, [], { ...source, windowComplete: false }),
		resolve(observation, [], {
			...source,
			gameVersions: { from: "7.1", through: "7.1" },
		}),
	])
		assert.deepEqual(invalid, { status: "failed", code: "INVALID_CITATION" });
	for (const fake of ["你已经持有丁。", "建议见 https://example.test/forged"]) {
		assert.deepEqual(resolve({ ...observation, text: fake }), {
			status: "failed",
			code: "INVALID_ANSWER_STRUCTURE",
			validationDetail: "invalid_field:$.observations[0].text",
		});
	}
});

test("装备和命座由独立前提判定，不要求原文出现整队", () => {
	const inventory: FrozenQueryScope = {
		...scope,
		teamMode: "inventory",
		playerConstraints: {
			targetHolding: "owned",
			teammates: { kind: "known", items: ["乙", "丙", "丁"] },
			equipment: { kind: "unknown" },
		},
	};
	const source = {
		...reference,
		requiredCharacterIds: [],
		requiredEquipmentIds: [],
		conditions: ["甲需达到二命"],
		citation: { ...reference.citation, excerpt: "辅助位：丁可减抗。" },
	};
	const result = validateGuideAnswer(
		draft([recommendation]),
		inventory,
		"t1",
		testView(source),
	);
	assert.ok(result.status === "answered");
	assert.match(result.text, /参考配队：[\s\S]*适用前提：甲需达到二命/u);
	assert.doesNotMatch(result.text, /可用配队/u);
});

const conditionFailure = (validationDetail: GuideConditionDetail) => ({
	status: "failed",
	code: "INVALID_ANSWER_CONDITIONS",
	validationDetail,
});

test("目标遗漏仍拒绝，有依据但缺人或缺装备标为参考", () => {
	const clean = {
		...reference,
		requiredCharacterIds: [],
		requiredEquipmentIds: [],
	};
	const missingTarget = {
		...recommendation,
		team: recommendation.team.map((slot) =>
			slot.characterId === "甲" ? { ...slot, characterId: "戊" } : slot,
		),
	};
	assert.deepEqual(
		validateGuideAnswer(draft([missingTarget]), scope, "t1", testView(clean)),
		conditionFailure("recommendation:target_missing"),
	);
	const excluded = {
		...scope,
		playerConstraints: {
			...scope.playerConstraints,
			excludedCharacters: ["乙"],
			excludedEquipment: ["试验弓"],
		},
	};
	const excludedTeam = validateGuideAnswer(
		draft([recommendation]),
		excluded,
		"t1",
		testView({ ...reference, requiredCharacterIds: [] }),
	);
	assert.ok(excludedTeam.status === "answered");
	assert.match(excludedTeam.text, /参考配队/u);
	assert.match(excludedTeam.text, /尚未持有角色「乙」/u);
	assert.match(excludedTeam.text, /尚未持有装备「试验弓」/u);
	assert.doesNotMatch(excludedTeam.text, /可用配队/u);
});

test("资料不足诊断区分候选没读完、缺口未确认和已有可用方案", () => {
	const proposal = draft(
		[],
		[{ kind: "insufficient_evidence", text: "不进入诊断的模型正文" }],
	);
	assert.deepEqual(
		validateGuideAnswer(proposal, scope, "t1", {
			...testView({
				...reference,
				citation: { ...reference.citation, excerpt: "辅助位：丁。" },
			}),
			gap: false,
			hasUnreadCandidates: true,
		}),
		conditionFailure("insufficient_evidence:unread_candidates"),
	);
	assert.deepEqual(
		validateGuideAnswer(proposal, scope, "t1", {
			...testView(),
			gap: false,
		}),
		conditionFailure("insufficient_evidence:gap_not_established"),
	);

	assert.deepEqual(
		validateGuideAnswer(proposal, scope, "t1", testView(reference)),
		conditionFailure("insufficient_evidence:evidence_not_used"),
	);
});

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
		const unconfirmed = target === "甲" ? "替代弓" : "试验弓";
		const excerpt = [
			...[owned[0], excluded, unconfirmed].map(
				(id) => `阵容：甲＋乙＋丙＋丁；为${target}装备${id}。`,
			),
			"阵容：甲＋乙＋丙＋（丁/戊）。",
		].join("\n");
		const read = testView({
			...source,
			entityId: target,
			citation: { ...source.citation, excerpt },
		});
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
		assert.ok(ok.status === "answered");
		assert.match(ok.text, /可用配队/u);
		assert.ok(ok.text.includes(`${target}（${owned[0]}）`));
		const alternate = {
			...recommendation,
			team: recommendation.team.map((slot) =>
				slot.characterId === "丁" ? { ...slot, characterId: "戊" } : slot,
			),
		};
		for (const item of [
			withGear([excluded]),
			withGear([unconfirmed]),
			alternate,
		]) {
			const answer = submit(item);
			assert.ok(answer.status === "answered");
			assert.match(answer.text, /参考配队/u);
			assert.doesNotMatch(answer.text, /可用配队/u);
		}
		const ordered = validateGuideAnswer(
			draft([withGear([excluded]), withGear([owned[0]])]),
			inventory,
			"t1",
			read,
		);
		assert.ok(ordered.status === "answered");
		assert.match(
			ordered.text,
			/^配队建议（模型整理）：\n可用配队：[\s\S]+参考配队：/u,
		);
		const unconfirmedSlot = submit({
			...recommendation,
			team: recommendation.team.map((slot) =>
				slot.characterId === "丙" ? { ...slot, characterId: "戊" } : slot,
			),
		});
		assert.ok(unconfirmedSlot.status === "answered");
		assert.match(unconfirmedSlot.text, /参考配队/u);
		assert.doesNotMatch(unconfirmedSlot.text, /可用配队/u);
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

test("必需装备持有未知时仍给参考，未知前提不能标可用", () => {
	const inventory: FrozenQueryScope = {
		...scope,
		teamMode: "inventory",
		playerConstraints: {
			targetHolding: "owned",
			teammates: { kind: "known", items: ["乙", "丙", "丁"] },
			equipment: { kind: "unknown" },
		},
	};
	const input = draft([recommendation]);
	const only = validateGuideAnswer(input, inventory, "t1", testView(reference));
	assert.ok(only.status === "answered");
	assert.match(only.text, /参考配队/u);
	assert.match(only.text, /持有状态未确认/u);
	const held = {
		...inventory,
		playerConstraints: {
			...inventory.playerConstraints,
			ownedEquipment: ["试验弓"],
		},
	};
	const available = validateGuideAnswer(input, held, "t1", testView(reference));
	assert.ok(available.status === "answered");
	assert.match(available.text, /可用配队/u);
	const unknown = validateGuideAnswer(
		input,
		held,
		"t1",
		testView({ ...reference, applicabilityUnknown: true }),
	);
	assert.ok(unknown.status === "answered");
	assert.match(unknown.text, /适用条件未确认/u);
	assert.doesNotMatch(unknown.text, /可用配队/u);
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
	const paraphrase = sides.map((side) => ({
		...side,
		conditions: ["必须拥有试验弓"],
	}));
	assert.deepEqual(
		validateGuideAnswer(
			draft([], [{ kind: "conflict", sides: paraphrase }]),
			scope,
			"t1",
			testView(reference, second),
		),
		conditionFailure("conflict:premise_not_matched_exactly"),
	);
	assert.equal(
		validateGuideAnswer(
			input,
			{
				...scope,
				playerConstraints: {
					...scope.playerConstraints,
					excludedEquipment: ["试验弓"],
				},
			},
			"t1",
			testView(reference, second),
		).status,
		"answered",
	);
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
