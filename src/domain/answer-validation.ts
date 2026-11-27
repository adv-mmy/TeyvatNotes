import {
	type FrozenQueryScope,
	inventoryFit,
	resolveTerm,
	type TerminologyEntry,
	versionApplies,
} from "./query-scope.js";

export interface GuideCitation {
	readonly source: string;
	readonly url: string;
	readonly version: string;
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
	readonly gameVersions: { readonly from: string; readonly through: string };
	readonly windowComplete: boolean;
	readonly chunkId: string;
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
	readonly supplementGap?: SupplementGap;
	readonly evidence: ReadonlyMap<string, GuideEvidence>;
	readonly gap: boolean;
	readonly readAllCandidates: boolean;
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
			readonly status: "needs_clarification";
			readonly text: string;
			readonly equipmentId: string;
	  }
	| {
			readonly status: "failed";
			readonly code: "INVALID_ANSWER_STRUCTURE";
			readonly validationDetail: GuideStructureDetail;
	  }
	| {
			readonly status: "failed";
			readonly code: "INVALID_CITATION" | "INVALID_ANSWER_CONDITIONS";
	  };

const invalid = (
	code: "INVALID_CITATION" | "INVALID_ANSWER_CONDITIONS",
): GuideValidation => ({ status: "failed", code });
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
): GuideStructureDetail | undefined {
	const missing = keys.find(
		(key) => key !== "evidenceIds" && !Object.hasOwn(value, key),
	);
	if (missing) return `missing_field:${path}.${missing}`;
	if (Object.keys(value).some((key) => !keys.includes(key)))
		return `unexpected_fields:${path}`;
	// evidenceIds 仍由 refs 校验并沿用 INVALID_CITATION。
	return undefined;
}

function relevant(
	scope: FrozenQueryScope,
	turnId: string,
	evidence: GuideEvidence,
): boolean {
	return (
		evidence.turnId === turnId &&
		evidence.scopeId === scope.scopeId &&
		evidence.entityId === scope.entityId &&
		evidence.guideKind === scope.guideKind &&
		evidence.windowComplete &&
		versionApplies(scope.gameVersion, evidence.gameVersions)
	);
}

function canUse(scope: FrozenQueryScope, evidence: GuideEvidence): boolean {
	return inventoryFit(scope, evidence) === "usable";
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
			!relevant(scope, turnId, evidence)
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

type TeamSlot = {
	readonly characterId: string;
	readonly equipmentIds: readonly string[];
};

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
): string {
	const lineup = team
		.map(({ characterId, equipmentIds }) =>
			equipmentIds.length
				? `${characterId}（${equipmentIds.join("、")}）`
				: characterId,
		)
		.join("＋");
	const general = scope.teamMode === "general";
	const conditions = sources.flatMap((record) => record.conditions);
	if (general) {
		conditions.unshift("需持有阵容中的四名角色及所列装备");
		if (scope.playerConstraints.targetHolding === "not_owned")
			conditions.push(`尚未持有目标角色「${scope.entityId}」`);
	}
	return show(
		`${general ? "参考配队" : "可用配队"}：${lineup}`,
		conditions,
		sources,
	);
}

/** 领域层只读本轮成功回读证据；原文是否语义支持结论由固定用例评测。 */
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
	const root = fieldShape(draft, "$", ["recommendations", "gaps"]);
	if (root) return structure(root);
	if (!Array.isArray(draft.recommendations))
		return structure("invalid_field:$.recommendations");
	if (!Array.isArray(draft.gaps)) return structure("invalid_field:$.gaps");
	if (!draft.recommendations.length && !draft.gaps.length)
		return structure("status_conflict:both_empty");
	if (draft.recommendations.length && draft.gaps.length)
		return structure("status_conflict:both_nonempty");
	const available = [...view.evidence.values()].filter((item) =>
		relevant(scope, turnId, item),
	);
	const records: GuideEvidence[] = [];
	const output: string[] = [];
	for (const [i, item] of (draft.recommendations as unknown[]).entries()) {
		const path = `$.recommendations[${i}]`;
		if (!object(item)) return structure(`invalid_field:${path}`);
		const fields = fieldShape(item, path, ["team", "evidenceIds"]);
		if (fields) return structure(fields);
		const result = validateTeam(item.team, `${path}.team`, terms);
		if ("detail" in result) return structure(result.detail);
		const { team } = result;
		const sources = refs(item.evidenceIds, scope, turnId, view);
		if (!sources) return invalid("INVALID_CITATION");
		const selected = {
			requiredCharacterIds: team.map((slot) => slot.characterId),
			requiredEquipmentIds: team.flatMap((slot) => slot.equipmentIds),
		};
		if (
			!sources.every((record) => canUse(scope, record)) ||
			!selected.requiredCharacterIds.includes(scope.entityId) ||
			inventoryFit(scope, selected) !== "usable"
		)
			return invalid("INVALID_ANSWER_CONDITIONS");
		output.push(showTeam(scope, team, sources));
		records.push(...sources);
	}
	if (output.length)
		return {
			status: "answered",
			text: output.join("\n\n"),
			citations: cited(records),
		};
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
		return view.gap && !available.some((item) => canUse(scope, item))
			? {
					status: "insufficient_evidence",
					text: "没有取得适用且可引用的完整攻略证据。",
					...(view.supplementGap ? { supplementGap: view.supplementGap } : {}),
				}
			: invalid("INVALID_ANSWER_CONDITIONS");
	}
	if (gap.kind === "equipment_required") {
		const fields = fieldShape(gap, gapPath, [
			"kind",
			"equipmentId",
			"evidenceIds",
		]);
		if (fields) return structure(fields);
		if (!text(gap.equipmentId))
			return structure(`invalid_field:${gapPath}.equipmentId`);
		const sources = refs(gap.evidenceIds, scope, turnId, view);
		if (!sources) return invalid("INVALID_CITATION");
		const equipmentId = gap.equipmentId;
		if (
			scope.teamMode !== "inventory" ||
			(scope.playerConstraints.equipment.kind === "known" &&
				scope.playerConstraints.equipment.items.includes(equipmentId)) ||
			!view.readAllCandidates ||
			available.some((item) => canUse(scope, item)) ||
			!sources.some(
				(item) =>
					item.requiredEquipmentIds.includes(equipmentId) &&
					!scope.playerConstraints.ownedEquipment?.includes(equipmentId) &&
					inventoryFit(scope, item) === "unknown_equipment",
			)
		)
			return invalid("INVALID_ANSWER_CONDITIONS");
		return {
			status: "needs_clarification",
			equipmentId,
			text: `回读的配队方案需要「${equipmentId}」。请确认是否持有：可输入「我有${equipmentId}」或「我没有${equipmentId}」，也可填写装备清单。「装备：无」表示没有任何装备。`,
		};
	}
	if (gap.kind !== "conflict") return structure("unsupported_gap_kind");
	const fields = fieldShape(gap, gapPath, ["kind", "sides"]);
	if (fields) return structure(fields);
	if (!Array.isArray(gap.sides) || gap.sides.length !== 2)
		return structure(`invalid_field:${gapPath}.sides`);
	const chunks = new Set<string>();
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
			return invalid("INVALID_CITATION");
		if (
			!premises(item.conditions, sources) ||
			!sources.every((record) => canUse(scope, record))
		)
			return invalid("INVALID_ANSWER_CONDITIONS");
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
