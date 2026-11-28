import {
	type FrozenQueryScope,
	type GameVersions,
	type InventoryRejection,
	inventoryRejection,
	matchesVersionLayer,
	resolveTerm,
	type TerminologyEntry,
	versionLayers,
} from "./query-scope.js";

export interface GuideCitation {
	readonly source: string;
	readonly url: string;
	readonly version: string | null;
	readonly documentVersionId: string;
	readonly location: string;
	readonly excerpt: string;
}
export interface GuideEvidence {
	readonly evidenceId: string;
	readonly turnId: string;
	readonly scopeId: string;
	readonly entityId: string;
	readonly guideKind: "team";
	readonly gameVersions: GameVersions;
	readonly windowComplete: boolean;
	readonly chunkId: string;
	readonly fromWeb?: boolean;
	readonly applicabilityUnknown?: boolean;
	readonly requiredCharacterIds: readonly string[];
	readonly requiredEquipmentIds: readonly string[];
	readonly conditions: readonly string[];
	readonly citation: GuideCitation;
}

const supplementGaps = [
	"recheck_miss",
	"no_results",
	"source_restricted",
	"no_readable_body",
	"metadata_incomplete",
	"scope_mismatch",
	"inventory_mismatch",
] as const;
export type SupplementGap = (typeof supplementGaps)[number];
export function isSupplementGap(value: unknown): value is SupplementGap {
	return supplementGaps.some((reason) => reason === value);
}

export interface GuideEvidenceView {
	readonly versionLayer?: string | null;
	readonly supplementGap?: SupplementGap;
	readonly evidence: ReadonlyMap<string, GuideEvidence>;
	readonly gap: boolean;
	readonly hasUnreadCandidates: boolean;
}

export type GuideStructureDetail =
	| "json_invalid"
	| "json_code_fence"
	| "root_not_object"
	| "status_conflict:both_empty"
	| "status_conflict:both_nonempty"
	| "invalid_gap_count"
	| "unsupported_gap_kind"
	| `${"missing_field" | "invalid_field" | "unexpected_fields"}:${string}`;

export type GuideConditionDetail =
	| "recommendation:target_missing"
	| "insufficient_evidence:unread_candidates"
	| "insufficient_evidence:gap_not_established"
	| "insufficient_evidence:evidence_not_used"
	| "conflict:premise_not_matched_exactly";

export type GuideValidationDetail = GuideStructureDetail | GuideConditionDetail;

export type GuideValidation =
	| {
			readonly status: "answered";
			readonly text: string;
			readonly citations: readonly GuideCitation[];
	  }
	| {
			readonly status: "insufficient_evidence";
			readonly text: string;
			readonly supplementGap?: SupplementGap;
	  }
	| {
			readonly status: "failed";
			readonly code: "INVALID_ANSWER_STRUCTURE";
			readonly validationDetail: GuideStructureDetail;
	  }
	| {
			readonly status: "failed";
			readonly code: "INVALID_ANSWER_CONDITIONS";
			readonly validationDetail: GuideConditionDetail;
	  }
	| {
			readonly status: "failed";
			readonly code: "INVALID_CITATION";
	  };

const invalidCitation = (): GuideValidation => ({
	status: "failed",
	code: "INVALID_CITATION",
});
const condition = (
	validationDetail: GuideConditionDetail,
): GuideValidation => ({
	status: "failed",
	code: "INVALID_ANSWER_CONDITIONS",
	validationDetail,
});
const structure = (
	validationDetail: GuideStructureDetail,
): GuideValidation => ({
	status: "failed",
	code: "INVALID_ANSWER_STRUCTURE",
	validationDetail,
});

function object(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}
function text(value: unknown): value is string {
	return typeof value === "string" && value.trim() !== "";
}
function strings(value: unknown): value is string[] {
	return (
		Array.isArray(value) &&
		value.every(text) &&
		new Set(value).size === value.length
	);
}
/** 诊断路径与字段名只来自下方固定协议；不回显模型提供的属性名或值。 */
function fieldShape(
	value: Record<string, unknown>,
	path: string,
	keys: readonly string[],
	optional: readonly string[] = [],
): GuideStructureDetail | undefined {
	const missing = keys.find(
		(key) => key !== "evidenceIds" && !Object.hasOwn(value, key),
	);
	if (missing) return `missing_field:${path}.${missing}`;
	if (
		Object.keys(value).some(
			(key) => !keys.includes(key) && !optional.includes(key),
		)
	)
		return `unexpected_fields:${path}`;
	// evidenceIds 仍由 refs 校验并沿用 INVALID_CITATION。
	return undefined;
}

function relevant(
	scope: FrozenQueryScope,
	turnId: string,
	evidence: GuideEvidence,
	layer: string | null | undefined,
): boolean {
	const selected =
		layer === undefined && scope.versionScope.kind === "exact"
			? scope.versionScope.version
			: layer;
	return (
		selected !== undefined &&
		versionLayers(scope.versionScope).includes(selected) &&
		evidence.turnId === turnId &&
		evidence.scopeId === scope.scopeId &&
		evidence.entityId === scope.entityId &&
		evidence.guideKind === scope.guideKind &&
		evidence.windowComplete &&
		matchesVersionLayer(selected, evidence.gameVersions) &&
		(evidence.citation.version === null) === (evidence.gameVersions === null)
	);
}

function refs(
	value: unknown,
	scope: FrozenQueryScope,
	turnId: string,
	view: GuideEvidenceView,
): GuideEvidence[] | undefined {
	if (!strings(value) || !value.length) return undefined;
	const records: GuideEvidence[] = [];
	for (const id of value) {
		const evidence = view.evidence.get(id);
		if (
			!evidence ||
			evidence.evidenceId !== id ||
			!relevant(scope, turnId, evidence, view.versionLayer)
		)
			return undefined;
		records.push(evidence);
	}
	return records;
}

function premises(
	conditions: readonly string[],
	evidence: readonly GuideEvidence[],
): boolean {
	return evidence.every((record) =>
		record.conditions.every((name) => conditions.includes(name)),
	);
}

function cited(records: readonly GuideEvidence[]): readonly GuideCitation[] {
	return [
		...new Map(
			records.map((item) => [item.evidenceId, { ...item.citation }]),
		).values(),
	];
}

function show(
	text: string,
	declared: readonly string[],
	sources: readonly GuideEvidence[],
): string {
	const conditions = [
		...new Set([
			...declared,
			...sources.flatMap((source) => [
				...source.requiredCharacterIds.map((id) => `需持有角色「${id}」`),
				...source.requiredEquipmentIds.map((id) => `需持有装备「${id}」`),
			]),
		]),
	];
	return conditions.length
		? `${text}\n适用前提：${conditions.join("；")}`
		: text;
}

function displayedConditions(sources: readonly GuideEvidence[]): string[] {
	const conditions = sources.flatMap((record) => record.conditions);
	if (sources.some((record) => record.applicabilityUnknown))
		conditions.push("适用条件未确认");
	return conditions;
}

const observationLabels = {
	teammate: "队友选择",
	role: "位置分工",
	restriction: "条件提示",
} as const;

function validExcerpt(value: unknown): value is string {
	return (
		text(value) &&
		value.length <= 360 &&
		!/\p{Cc}/u.test(value.replace(/[\n\t]/gu, ""))
	);
}

function validObservation(value: unknown): value is string {
	return (
		text(value) &&
		value.length <= 420 &&
		!/\p{C}|https?:\/\/|www\.|\]\s*\(|(?:你|您|玩家)(?:当前|已经|确认|明确|已)?(?:持有|拥有)|(?:当前|现在|已确认)可用/iu.test(
			value,
		)
	);
}

function prepareObservations(
	items: readonly unknown[],
	scope: FrozenQueryScope,
	turnId: string,
	view: GuideEvidenceView,
):
	| { output: string[]; citations: Map<string, GuideCitation[]> }
	| { error: GuideValidation } {
	const output: string[] = [];
	const citations = new Map<string, GuideCitation[]>();
	for (const [i, item] of items.entries()) {
		const at = `$.observations[${i}]`;
		if (!object(item)) return { error: structure(`invalid_field:${at}`) };
		const shape = fieldShape(item, at, [
			"kind",
			"text",
			"evidenceId",
			"excerpt",
		]);
		if (shape) return { error: structure(shape) };
		if (
			item.kind !== "teammate" &&
			item.kind !== "role" &&
			item.kind !== "restriction"
		)
			return { error: structure(`invalid_field:${at}.kind`) };
		if (!validObservation(item.text))
			return { error: structure(`invalid_field:${at}.text`) };
		if (!validExcerpt(item.excerpt))
			return { error: structure(`invalid_field:${at}.excerpt`) };
		const source = refs([item.evidenceId], scope, turnId, view)?.[0];
		if (!source?.citation.excerpt.includes(item.excerpt))
			return { error: invalidCitation() };
		const line = `${observationLabels[item.kind]}：${item.text.trim()}`;
		if (!output.includes(line)) output.push(line);
		const entries = citations.get(source.evidenceId) ?? [];
		if (!entries.some((cite) => cite.excerpt === item.excerpt))
			entries.push({ ...source.citation, excerpt: item.excerpt });
		citations.set(source.evidenceId, entries);
	}
	return { output, citations };
}

type TeamSlot = {
	readonly characterId: string;
	readonly equipmentIds: readonly string[];
};

/** 仅供来源适配器规范化原文，回答不按此格式检查阵容。 */
export function readExplicitTeam(
	text: string,
	terms: readonly TerminologyEntry[],
): { team: readonly string[]; equipmentNote?: string } | undefined {
	const line = text
		.trim()
		.replace(/^[*#;:]\s*/u, "")
		.replace(/。$/u, "");
	const label =
		/^(?:(?:四人|完整|推荐|参考|示例)?(?:阵容|队伍|配队)(?:示例)?\s*(?:为|[:：])|推荐(?:使用|由)?\s*)\s*/u;
	const [, literal, ending] =
		/^([^。；;]+)(?:[。；;]\s*(.+))?$/u.exec(line.replace(label, "")) ?? [];
	if (!literal) return;
	const [, before, after] =
		/^(.+?)\s*(?:可?组队|组成(?:一支)?队伍)(?:[，,]\s*(.+))?$/u.exec(literal) ??
		[];
	if (!label.test(line) && !before && literal.split(/[+＋]/u).length !== 4)
		return;
	const stated = (before ?? literal).replace(/[＋+]/gu, "＋");
	const slots = stated.includes("＋")
		? stated.split("＋")
		: stated.split(/[、，,]/u);
	if (slots.length !== 4) return;
	const team = slots.map((field) =>
		resolveTerm(field.trim().replace(/^[（(](.*)[）)]$/u, "$1"), terms, [
			"character",
		]),
	);
	if (!team.every((id) => id !== undefined) || new Set(team).size !== 4) return;
	const note = [after, ending].filter(Boolean).join("；");
	return { team, ...(note ? { equipmentNote: note } : {}) };
}

function validateTeam(
	value: unknown,
	path: string,
	terms: readonly TerminologyEntry[],
): { team: readonly TeamSlot[] } | { detail: GuideStructureDetail } {
	if (!Array.isArray(value) || value.length !== 4)
		return { detail: `invalid_field:${path}` };
	const team: TeamSlot[] = [];
	const characters = new Set<string>();
	for (const [i, slot] of (value as unknown[]).entries()) {
		const at = `${path}[${i}]`;
		if (!object(slot)) return { detail: `invalid_field:${at}` };
		const fields = fieldShape(slot, at, ["characterId", "equipmentIds"]);
		if (fields) return { detail: fields };
		const id = text(slot.characterId)
			? resolveTerm(slot.characterId, terms, ["character"])
			: undefined;
		if (!id || characters.has(id))
			return { detail: `invalid_field:${at}.characterId` };
		if (!strings(slot.equipmentIds))
			return { detail: `invalid_field:${at}.equipmentIds` };
		const equipment = new Set<string>();
		let weapons = 0;
		for (const label of slot.equipmentIds) {
			const name = resolveTerm(label, terms, ["weapon", "artifact"]);
			if (
				!name ||
				equipment.has(name) ||
				(resolveTerm(label, terms, ["weapon"]) && ++weapons > 1)
			)
				return { detail: `invalid_field:${at}.equipmentIds` };
			equipment.add(name);
		}
		characters.add(id);
		team.push({ characterId: id, equipmentIds: [...equipment] });
	}
	return { team };
}

function showTeam(
	scope: FrozenQueryScope,
	team: readonly TeamSlot[],
	sources: readonly GuideEvidence[],
	terms: readonly TerminologyEntry[],
): { text: string; usable: boolean } {
	const lineup = team
		.map(({ characterId, equipmentIds }) =>
			equipmentIds.length
				? `${characterId}（${equipmentIds.join("、")}）`
				: characterId,
		)
		.join("＋");
	const characters = [
		...new Set([
			...team.map(({ characterId }) => characterId),
			...sources.flatMap((r) => r.requiredCharacterIds),
		]),
	];
	const chosenEquipment = team.flatMap((slot) => slot.equipmentIds);
	const equipment = [
		...new Set([
			...chosenEquipment,
			...sources.flatMap((r) => r.requiredEquipmentIds),
		]),
	];
	const c = scope.playerConstraints;
	const issues: string[] = [];
	const add = (label: string, ids: readonly string[]) => {
		if (ids.length) issues.push(`${label}「${ids.join("、")}」`);
	};
	const roleAvoided = characters.filter((id) =>
		c.avoidedCharacters?.includes(id),
	);
	const gearAvoided = equipment.filter((id) =>
		c.avoidedEquipment?.includes(id),
	);
	add("本轮排除角色", roleAvoided);
	add("本轮排除装备", gearAvoided);
	add(
		"尚未持有角色",
		characters.filter(
			(id) =>
				!roleAvoided.includes(id) &&
				(c.excludedCharacters?.includes(id) ||
					(id === scope.entityId && c.targetHolding === "not_owned")),
		),
	);
	add(
		"尚未持有装备",
		equipment.filter(
			(id) => !gearAvoided.includes(id) && c.excludedEquipment?.includes(id),
		),
	);
	if (c.noTeammates && characters.some((id) => id !== scope.entityId))
		issues.push("已确认队友清单为空");
	if (
		c.equipment.kind === "known" &&
		!c.equipment.items.length &&
		equipment.length
	)
		issues.push("已确认装备清单为空");
	const conditions = displayedConditions(sources);
	const unstated = sources.some((record) => record.applicabilityUnknown);
	const unverifiedCondition = sources.some((record) =>
		record.conditions.some((text) => {
			const name =
				/^(?:(?:须|必须|需|需要)?持有(?:角色|装备)?\s*|仅装备\s*)(.+?)(?:时)?$/u.exec(
					text.trim(),
				)?.[1];
			return !(
				name &&
				(record.requiredCharacterIds.includes(
					resolveTerm(name, terms, ["character"]) ?? "",
				) ||
					record.requiredEquipmentIds.includes(
						resolveTerm(name, terms, ["weapon", "artifact"]) ?? "",
					))
			);
		}),
	);
	const fit = inventoryRejection(scope, {
		requiredCharacterIds: characters,
		requiredEquipmentIds: equipment,
	});
	if (scope.teamMode === "inventory" && fit && !issues.length) {
		const explanations: Record<InventoryRejection, string> = {
			teammates_disallowed: "已确认没有可用队友",
			character_excluded: "阵容包含未持有或已排除的角色",
			target_not_owned: "尚未持有目标角色",
			target_unconfirmed: "目标角色持有状态未确认",
			teammates_unconfirmed: "队友持有状态未确认",
			character_not_in_confirmed_list: "部分角色不在已确认队友清单",
			equipment_excluded: "阵容包含未持有或已排除的装备",
			equipment_list_empty: "已确认装备清单为空",
			equipment_not_in_allowed_list: "部分装备不在本轮限用清单",
			equipment_unconfirmed: "必要装备持有状态未确认",
		};
		issues.push(explanations[fit]);
	}
	if (scope.teamMode === "inventory" && unverifiedCondition && !unstated)
		issues.push("原文明示的前提尚未确认满足");
	const usable =
		scope.teamMode === "inventory" &&
		!fit &&
		!unstated &&
		!unverifiedCondition &&
		!issues.length;
	if (scope.teamMode === "general")
		conditions.unshift("需持有阵容中的四名角色及所列装备");
	const text = show(
		`${usable ? "可用配队" : "参考配队"}：${lineup}`,
		conditions,
		sources,
	);
	return {
		usable,
		text: issues.length ? `${text}\n当前可用性：${issues.join("；")}。` : text,
	};
}

/** 引用必须来自本轮完整回读；模型整理的组合只检查结构和库存可用性。 */
export function validateGuideAnswer(
	raw: string,
	scope: FrozenQueryScope,
	turnId: string,
	view: GuideEvidenceView,
	terms: readonly TerminologyEntry[],
): GuideValidation {
	let draft: unknown;
	try {
		draft = JSON.parse(raw);
	} catch {
		return structure(
			/^\s*(?:```|~~~)/u.test(raw) ? "json_code_fence" : "json_invalid",
		);
	}
	if (!object(draft)) return structure("root_not_object");
	const root = fieldShape(
		draft,
		"$",
		["recommendations", "gaps"],
		["observations"],
	);
	if (root) return structure(root);
	if (!Array.isArray(draft.recommendations))
		return structure("invalid_field:$.recommendations");
	if (!Array.isArray(draft.gaps)) return structure("invalid_field:$.gaps");
	const observations = Object.hasOwn(draft, "observations")
		? draft.observations
		: [];
	if (!Array.isArray(observations) || observations.length > 8)
		return structure("invalid_field:$.observations");
	const hasAnswer = draft.recommendations.length > 0 || observations.length > 0;
	if (!hasAnswer && !draft.gaps.length)
		return structure("status_conflict:both_empty");
	if (hasAnswer && draft.gaps.length)
		return structure("status_conflict:both_nonempty");

	if (hasAnswer) {
		const notes = prepareObservations(observations, scope, turnId, view);
		if ("error" in notes) return notes.error;
		const sources = new Map<string, GuideEvidence>();
		const teams: { text: string; usable: boolean }[] = [];
		for (const [i, item] of (draft.recommendations as unknown[]).entries()) {
			const path = `$.recommendations[${i}]`;
			if (!object(item)) return structure(`invalid_field:${path}`);
			const fields = fieldShape(item, path, ["team", "evidenceIds"]);
			if (fields) return structure(fields);
			const result = validateTeam(item.team, `${path}.team`, terms);
			if ("detail" in result) return structure(result.detail);
			const { team } = result;
			const records = refs(item.evidenceIds, scope, turnId, view);
			if (!records) return invalidCitation();
			if (!team.some((slot) => slot.characterId === scope.entityId))
				return condition("recommendation:target_missing");
			for (const record of records) sources.set(record.evidenceId, record);
			teams.push(showTeam(scope, team, records, terms));
		}
		teams.sort((a, b) => Number(b.usable) - Number(a.usable));
		const sections: string[] = [];
		if (notes.output.length) {
			const noteSources = [...notes.citations.keys()].flatMap((id) => {
				const record = view.evidence.get(id);
				return record ? [record] : [];
			});
			sections.push(
				show(
					`配队思路：\n${notes.output.join("\n")}`,
					displayedConditions(noteSources),
					noteSources,
				),
			);
		}
		if (teams.length)
			sections.push(
				`配队建议（模型整理）：\n${teams.map(({ text }) => text).join("\n\n")}`,
			);
		return {
			status: "answered",
			text: sections.join("\n\n"),
			citations: [
				...notes.citations.values(),
				[...sources.values()]
					.filter((source) => !notes.citations.has(source.evidenceId))
					.map((source) => source.citation),
			].flat(),
		};
	}
	if (draft.gaps.length !== 1) return structure("invalid_gap_count");
	const gapPath = "$.gaps[0]";
	const gap = draft.gaps[0];
	if (!object(gap)) return structure(`invalid_field:${gapPath}`);
	if (!Object.hasOwn(gap, "kind"))
		return structure(`missing_field:${gapPath}.kind`);
	if (gap.kind === "insufficient_evidence") {
		const fields = fieldShape(gap, gapPath, ["kind", "text"]);
		if (fields) return structure(fields);
		if (!text(gap.text)) return structure(`invalid_field:${gapPath}.text`);
		if (!view.gap)
			return condition(
				view.hasUnreadCandidates
					? "insufficient_evidence:unread_candidates"
					: "insufficient_evidence:gap_not_established",
			);
		if (
			[...view.evidence.values()].some((item) =>
				relevant(scope, turnId, item, view.versionLayer),
			)
		)
			return condition("insufficient_evidence:evidence_not_used");
		return {
			status: "insufficient_evidence",
			text: "没有取得适用且可引用的完整攻略证据。",
			...(view.supplementGap ? { supplementGap: view.supplementGap } : {}),
		};
	}
	if (gap.kind !== "conflict") return structure("unsupported_gap_kind");
	const fields = fieldShape(gap, gapPath, ["kind", "sides"]);
	if (fields) return structure(fields);
	if (!Array.isArray(gap.sides) || gap.sides.length !== 2)
		return structure(`invalid_field:${gapPath}.sides`);
	const chunks = new Set<string>();
	const records: GuideEvidence[] = [];
	const output: string[] = [];
	for (const [i, item] of (gap.sides as unknown[]).entries()) {
		const path = `${gapPath}.sides[${i}]`;
		if (!object(item)) return structure(`invalid_field:${path}`);
		const sideFields = fieldShape(item, path, [
			"conclusion",
			"conditions",
			"evidenceIds",
		]);
		if (sideFields) return structure(sideFields);
		if (!text(item.conclusion))
			return structure(`invalid_field:${path}.conclusion`);
		if (!strings(item.conditions))
			return structure(`invalid_field:${path}.conditions`);
		const sources = refs(item.evidenceIds, scope, turnId, view);
		if (!sources || sources.some((record) => chunks.has(record.chunkId)))
			return invalidCitation();
		if (!premises(item.conditions, sources))
			return condition("conflict:premise_not_matched_exactly");
		const { conclusion, conditions } = item;
		if (
			sources.some((record) => record.fromWeb) &&
			(!sources.some((record) =>
				record.citation.excerpt.includes(conclusion),
			) ||
				conditions.some(
					(condition) =>
						!sources.some(
							(record) =>
								record.conditions.includes(condition) ||
								record.citation.excerpt.includes(condition),
						),
				))
		)
			return invalidCitation();
		for (const source of sources) chunks.add(source.chunkId);
		output.push(show(item.conclusion, item.conditions, sources));
		records.push(...sources);
	}
	return {
		status: "answered",
		text: `已回读的攻略存在分歧，不能给出唯一结论。\n\n${output.map((value, i) => `观点 ${i + 1}：${value}`).join("\n\n")}`,
		citations: cited(records),
	};
}
