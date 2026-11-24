import { type FrozenQueryScope, versionApplies } from "./query-scope.js";

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

export interface GuideEvidenceView {
	readonly evidence: ReadonlyMap<string, GuideEvidence>;
	readonly gap: boolean;
	readonly readAllCandidates: boolean;
}

export type GuideValidation =
	| {
			readonly status: "answered";
			readonly text: string;
			readonly citations: readonly GuideCitation[];
	  }
	| { readonly status: "insufficient_evidence"; readonly text: string }
	| {
			readonly status: "needs_clarification";
			readonly text: string;
			readonly equipmentId: string;
	  }
	| {
			readonly status: "failed";
			readonly code:
				| "INVALID_ANSWER_STRUCTURE"
				| "INVALID_CITATION"
				| "INVALID_ANSWER_CONDITIONS";
	  };

const invalid = (
	code: Extract<GuideValidation, { status: "failed" }>["code"],
): GuideValidation => ({ status: "failed", code });
const structure = () => invalid("INVALID_ANSWER_STRUCTURE");

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
function only(
	value: Record<string, unknown>,
	keys: readonly string[],
): boolean {
	return Object.keys(value).every((key) => keys.includes(key));
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

function knownCharacters(
	scope: FrozenQueryScope,
	ids: readonly string[],
): boolean {
	const { teammates, targetHolding } = scope.playerConstraints;
	return ids.every((id) =>
		id === scope.entityId
			? targetHolding === "owned"
			: teammates.kind === "known" && teammates.items.includes(id),
	);
}

function canUse(scope: FrozenQueryScope, evidence: GuideEvidence): boolean {
	const { equipment, targetHolding } = scope.playerConstraints;
	if (
		targetHolding === "not_owned" &&
		evidence.requiredCharacterIds.includes(scope.entityId)
	)
		return false;
	return (
		scope.teamMode === "general" ||
		(knownCharacters(scope, evidence.requiredCharacterIds) &&
			evidence.requiredEquipmentIds.every(
				(id) => equipment.kind === "known" && equipment.items.includes(id),
			))
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

function show(text: string, conditions: readonly string[]): string {
	return conditions.length
		? `${text}\n适用前提：${conditions.join("；")}`
		: text;
}

/** 领域层只读本轮成功回读证据；原文是否语义支持结论由固定用例评测。 */
export function validateGuideAnswer(
	raw: string,
	scope: FrozenQueryScope,
	turnId: string,
	view: GuideEvidenceView,
): GuideValidation {
	let draft: unknown;
	try {
		draft = JSON.parse(raw);
	} catch {
		return structure();
	}
	if (
		!object(draft) ||
		!only(draft, ["recommendations", "gaps"]) ||
		!Array.isArray(draft.recommendations) ||
		!Array.isArray(draft.gaps) ||
		draft.recommendations.length + draft.gaps.length === 0 ||
		(draft.recommendations.length && draft.gaps.length)
	)
		return structure();
	const available = [...view.evidence.values()].filter((item) =>
		relevant(scope, turnId, item),
	);
	const records: GuideEvidence[] = [];
	const output: string[] = [];
	for (const item of draft.recommendations as unknown[]) {
		if (
			!object(item) ||
			!only(item, ["text", "conditions", "requiredEntityIds", "evidenceIds"]) ||
			!text(item.text) ||
			!strings(item.conditions) ||
			!strings(item.requiredEntityIds)
		)
			return structure();
		const sources = refs(item.evidenceIds, scope, turnId, view);
		if (!sources) return invalid("INVALID_CITATION");
		const required = new Set(
			sources.flatMap((item) => [
				...item.requiredCharacterIds,
				...item.requiredEquipmentIds,
			]),
		);
		if (
			!premises(item.conditions, sources) ||
			!sources.every((record) => canUse(scope, record)) ||
			item.requiredEntityIds.length !== required.size ||
			!item.requiredEntityIds.every((id) => required.has(id))
		)
			return invalid("INVALID_ANSWER_CONDITIONS");
		output.push(show(item.text, item.conditions));
		records.push(...sources);
	}
	if (output.length)
		return {
			status: "answered",
			text: output.join("\n\n"),
			citations: cited(records),
		};
	if (draft.gaps.length !== 1 || !object(draft.gaps[0])) return structure();
	const gap = draft.gaps[0];
	if (gap.kind === "insufficient_evidence") {
		if (!only(gap, ["kind", "text"]) || !text(gap.text)) return structure();
		return view.gap && !available.some((item) => canUse(scope, item))
			? {
					status: "insufficient_evidence",
					text: "没有取得适用且可引用的完整攻略证据。",
				}
			: invalid("INVALID_ANSWER_CONDITIONS");
	}
	if (gap.kind === "equipment_required") {
		if (
			!only(gap, ["kind", "equipmentId", "evidenceIds"]) ||
			!text(gap.equipmentId)
		)
			return structure();
		const sources = refs(gap.evidenceIds, scope, turnId, view);
		if (!sources) return invalid("INVALID_CITATION");
		const equipmentId = gap.equipmentId;
		if (
			scope.teamMode !== "inventory" ||
			scope.playerConstraints.equipment.kind !== "unknown" ||
			!view.readAllCandidates ||
			available.some((item) => canUse(scope, item)) ||
			!sources.some(
				(item) =>
					item.requiredEquipmentIds.includes(equipmentId) &&
					knownCharacters(scope, item.requiredCharacterIds),
			)
		)
			return invalid("INVALID_ANSWER_CONDITIONS");
		return {
			status: "needs_clarification",
			equipmentId,
			text: `回读的配队方案需要「${equipmentId}」。请确认你的装备清单（含该装备，如没有可列出其他已持有装备，或输入「装备：无」）。`,
		};
	}
	if (
		gap.kind !== "conflict" ||
		!only(gap, ["kind", "sides"]) ||
		!Array.isArray(gap.sides) ||
		gap.sides.length !== 2
	)
		return structure();
	const chunks = new Set<string>();
	for (const item of gap.sides as unknown[]) {
		if (
			!object(item) ||
			!only(item, ["conclusion", "conditions", "evidenceIds"]) ||
			!text(item.conclusion) ||
			!strings(item.conditions)
		)
			return structure();
		const sources = refs(item.evidenceIds, scope, turnId, view);
		if (!sources || sources.some((record) => chunks.has(record.chunkId)))
			return invalid("INVALID_CITATION");
		if (!premises(item.conditions, sources))
			return invalid("INVALID_ANSWER_CONDITIONS");
		for (const source of sources) chunks.add(source.chunkId);
		output.push(show(item.conclusion, item.conditions));
		records.push(...sources);
	}
	return {
		status: "answered",
		text: `已回读的攻略存在分歧，不能给出唯一结论。\n\n${output.map((value, i) => `观点 ${i + 1}：${value}`).join("\n\n")}`,
		citations: cited(records),
	};
}
