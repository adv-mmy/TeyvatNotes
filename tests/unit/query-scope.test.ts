import assert from "node:assert/strict";
import { test } from "node:test";
import {
	parseTerminology,
	readTerminology,
} from "../../src/bootstrap/config.js";
import {
	applySupplement,
	inventoryFit,
	parseQuestion,
	parseSupplement,
	resolveScope,
} from "../../src/domain/query-scope.js";

const terms = [
	{ category: "character", name: "角色甲", aliases: ["小甲"] },
	{ category: "character", name: "角色甲·冰", aliases: [] },
	{ category: "character", name: "角色乙", aliases: [] },
	{ category: "character", name: "队友丙", aliases: [] },
	{ category: "weapon", name: "试验弓", aliases: ["小弓"] },
	{ category: "artifact", name: "试验套", aliases: ["小套"] },
] as const;

test("未提及不是未持有；角色、武器别名按类别排除，歧义与互相矛盾先澄清", () => {
	const resolve = (s: string) =>
		resolveScope(parseQuestion(`小甲 7.1 怎么配队，${s}`, terms), {
			holdings: {},
		});
	const res = resolve("我没有角色乙、装备小弓");
	assert.equal(res.kind, "ready");
	if (res.kind !== "ready") return;
	assert.equal(res.scope.entityId, "角色甲");
	for (const [chars, gear, expected] of [
		[["角色乙"], [], "unavailable"],
		[[], ["试验弓"], "unavailable"],
		[["队友丙"], [], "usable"],
	] as const)
		assert.equal(
			inventoryFit(res.scope, {
				requiredCharacterIds: chars,
				requiredEquipmentIds: gear,
			}),
			expected,
		);
	for (const s of [
		"我没有未知装备",
		"我没有角色乙，我有角色乙",
		"我没有角色乙？",
	])
		assert.deepEqual(resolve(s), {
			kind: "missing",
			fields: ["inventoryFacts"],
		});
});

test("装备按类别校对，非装备或无法唯一确认的别名不能进入库存", () => {
	assert.equal(parseQuestion("试验弓 7.1 怎么配队", terms).entityId, undefined);
	assert.deepEqual(
		parseSupplement("装备：小弓、试验套", terms, ["equipment"])?.patch,
		{
			equipment: ["试验弓", "试验套"],
		},
	);
	assert.equal(
		parseSupplement("装备：角色乙", terms, ["equipment"]),
		undefined,
	);
	assert.equal(
		parseSupplement("装备：未知装备", terms, ["equipment"]),
		undefined,
	);
	const ambiguous = [
		...terms,
		{ category: "artifact", name: "另一个测试套", aliases: ["小弓"] },
	] as const;
	assert.equal(
		parseSupplement("装备：小弓", ambiguous, ["equipment"]),
		undefined,
	);
});

test("本地术语资源可读，缺失或非法结构有独立错误码", async () => {
	const glossary = await readTerminology();
	assert.equal(
		parseQuestion("奥萝塔 7.1 怎么配队", glossary).entityId,
		"奥黛塔",
	);
	assert.deepEqual(
		parseSupplement("装备：终末嗟叹之诗、绝缘之旗印", glossary, ["equipment"])
			?.patch,
		{
			equipment: ["终末嗟叹之诗", "绝缘之旗印"],
		},
	);
	assert.throws(
		() =>
			parseTerminology({
				revision: "test",
				terms: [{ category: "character", name: "A", aliases: [null] }],
			}),
		{ message: "TERMINOLOGY_INVALID" },
	);
	await assert.rejects(
		readTerminology(new URL("./never-present.json", import.meta.url)),
		{ message: "TERMINOLOGY_UNAVAILABLE" },
	);
});

test("未列出的装备仍可追问；限用清单和明确空清单不是未知", () => {
	const player = {
		holdings: { 角色甲: "owned" as const },
		teammates: ["队友丙"],
		equipment: ["其他装备"],
	};
	const draft = parseQuestion("小甲 7.1 按库存配队，本轮不用角色乙", terms);
	const rule = {
		requiredCharacterIds: ["队友丙"],
		requiredEquipmentIds: ["试验弓"],
	};
	for (const [input, expected] of [
		[draft, "unknown_equipment"],
		[{ ...draft, equipmentRequired: true }, "unavailable"],
		[{ ...draft, equipment: [] }, "unavailable"],
		[
			applySupplement(draft, {
				inventoryFacts: [
					{ category: "equipment", id: "试验弓", state: "owned" },
				],
			}),
			"usable",
		],
	] as const) {
		const result = resolveScope(input, player);
		assert.equal(result.kind, "ready");
		if (result.kind === "ready") {
			assert.equal(inventoryFit(result.scope, rule), expected);
			assert.deepEqual(result.scope.playerConstraints.excludedCharacters, [
				"角色乙",
			]);
		}
	}
});

test("标准名、别名和重叠名字都确定到同一角色，多个不同角色不擅自选目标", () => {
	assert.equal(parseQuestion("小甲 7.1 怎么配队", terms).entityId, "角色甲");
	assert.equal(
		parseQuestion("角色甲·冰 7.1 怎么配队", terms).entityId,
		"角色甲·冰",
	);
	assert.equal(
		parseQuestion("角色乙和小甲 7.1 怎么配队", terms).entityId,
		undefined,
	);
	assert.equal(
		parseQuestion("不存在的角色 7.1 怎么配队", terms).entityId,
		undefined,
	);
});

test("不从最新版、相邻版本或冲突库存意图推断已确认条件", () => {
	assert.equal(
		parseQuestion("小甲最新版怎么配队", terms).gameVersion,
		undefined,
	);
	assert.equal(
		parseQuestion("小甲暴击率 60.5% 怎么配队", terms).gameVersion,
		undefined,
	);
	assert.equal(
		parseQuestion("小甲 7.1 暴击率 60.5% 怎么配队", terms).gameVersion,
		"7.1",
	);
	assert.equal(
		parseQuestion("小甲 7.1 或 7.2 怎么配队", terms).gameVersion,
		undefined,
	);
	assert.equal(
		parseQuestion("小甲 7.1 通用还是按库存配队", terms).teamMode,
		undefined,
	);
	assert.deepEqual(
		resolveScope(parseQuestion("小甲怎么配队", terms), { holdings: {} }),
		{
			kind: "missing",
			fields: ["gameVersion"],
		},
	);
});

test("库存未知和明确为空不同，通用模式不要求库存", () => {
	const general = parseQuestion("小甲 7.1 怎么配队", terms);
	const direct = resolveScope(general, { holdings: {} });
	assert.equal(direct.kind, "ready");
	if (direct.kind === "ready")
		assert.deepEqual(direct.scope.playerConstraints.teammates, {
			kind: "unknown",
		});
	const personal = parseQuestion("小甲 7.1 按库存配队", terms);
	assert.deepEqual(resolveScope(personal, { holdings: {} }), {
		kind: "missing",
		fields: ["holding", "teammates"],
	});
	assert.deepEqual(resolveScope(personal, { holdings: {}, teammates: [] }), {
		kind: "no_team",
		reason: "no_teammates",
	});
	assert.deepEqual(
		resolveScope(personal, { holdings: { 角色甲: "not_owned" } }),
		{
			kind: "no_team",
			reason: "target_absent",
		},
	);
});

test("仅在用户限制装备时追问，按已确认状态过滤不能使用的队友而不是推断其余库存", () => {
	const draft = parseQuestion("小甲 7.1 按我的装备配队", terms);
	const inventory = {
		holdings: { 角色甲: "owned", 角色乙: "not_owned" },
		teammates: ["角色甲", "角色乙", "队友丙"],
	} as const;
	assert.deepEqual(resolveScope(draft, inventory), {
		kind: "missing",
		fields: ["equipment"],
	});
	const result = resolveScope({ ...draft, equipment: [] }, inventory);
	assert.equal(result.kind, "ready");
	if (result.kind === "ready") {
		assert.deepEqual(result.scope.playerConstraints.teammates, {
			kind: "known",
			items: ["队友丙"],
		});
		assert.deepEqual(result.scope.playerConstraints.equipment, {
			kind: "known",
			items: [],
		});
	}
});

test("补充条件必须明确且在请求范围内，切换角色不沿用原目标的持有状态", () => {
	assert.equal(
		parseSupplement("队友：不存在", terms, ["teammates"]),
		undefined,
	);
	assert.equal(
		parseSupplement("队友：无；队友：角色乙", terms, ["teammates"]),
		undefined,
	);
	assert.equal(parseSupplement("版本：7.2", terms, ["holding"]), undefined);
	assert.deepEqual(
		parseSupplement("角色：小甲；队友：无", terms, ["entityId", "teammates"])
			?.patch,
		{
			entityId: "角色甲",
			teammates: [],
		},
	);
	assert.deepEqual(
		applySupplement(
			{ entityId: "角色甲", holding: "owned", gameVersion: "7.1" },
			{
				entityId: "角色乙",
			},
		),
		{ entityId: "角色乙", gameVersion: "7.1" },
	);
});
