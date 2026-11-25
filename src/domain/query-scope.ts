export type TermCategory = "character" | "weapon" | "artifact";

export interface TerminologyEntry {
	readonly category: TermCategory;
	readonly name: string;
	readonly aliases: readonly string[];
}

export type TeamMode = "general" | "inventory";
export type Holding = "owned" | "not_owned";
export type ScopeField =
	| "entityId"
	| "gameVersion"
	| "goal"
	| "teamMode"
	| "holding"
	| "teammates"
	| "equipment";

export interface ScopeDraft {
	readonly entityId?: string;
	readonly gameVersion?: string;
	readonly goal?: string;
	readonly teamMode?: TeamMode;
	readonly holding?: Holding;
	readonly teammates?: readonly string[];
	readonly equipment?: readonly string[];
	readonly equipmentRequired?: boolean;
}

export interface PlayerInventory {
	readonly holdings: Readonly<Record<string, Holding>>;
	readonly teammates?: readonly string[];
}

type KnownItems =
	| { kind: "unknown" }
	| { kind: "known"; items: readonly string[] };
export interface QueryScope {
	readonly entityId: string;
	readonly gameVersion: string;
	readonly guideKind: "team";
	readonly goal: string;
	readonly teamMode: TeamMode;
	readonly playerConstraints: {
		readonly targetHolding: Holding | "unknown";
		readonly teammates: KnownItems;
		readonly equipment: KnownItems;
	};
}

export type FrozenQueryScope = Readonly<QueryScope & { scopeId: string }>;

export function versionApplies(
	version: string,
	range: { from: string; through: string },
): boolean {
	const value = gameVersionKey(version);
	const from = gameVersionKey(range.from);
	const through = gameVersionKey(range.through);
	return (
		value !== undefined &&
		from !== undefined &&
		through !== undefined &&
		from <= value &&
		value <= through
	);
}

/** 三段数字比较，7.10 大于 7.2，7.1 等价于 7.1.0。 */
export function gameVersionKey(value: string): number | undefined {
	if (!versionPattern.test(value)) return undefined;
	const [major = 0, minor = 0, patch = 0] = value.split(".").map(Number);
	return (major * 1000 + minor) * 1000 + patch;
}

export type ScopeResolution =
	| { readonly kind: "missing"; readonly fields: readonly ScopeField[] }
	| {
			readonly kind: "no_team";
			readonly reason: "target_absent" | "no_teammates";
	  }
	| { readonly kind: "ready"; readonly scope: QueryScope };

const versionPattern = /^\d{1,3}\.\d{1,3}(?:\.\d{1,3})?$/u;

function normalize(text: string): string {
	return text.normalize("NFKC").trim().toLowerCase();
}

function matchingNames(
	value: string,
	terms: readonly TerminologyEntry[],
	category: TermCategory,
	exact: boolean,
): string[] {
	const input = normalize(value);
	const candidates = terms
		.filter((term) => term.category === category)
		.flatMap(({ name, aliases }) =>
			[name, ...aliases]
				.map(normalize)
				.filter(Boolean)
				.flatMap((label) => {
					if (exact) return input === label ? [{ name, label, start: 0 }] : [];
					const hits: { name: string; label: string; start: number }[] = [];
					for (
						let start = input.indexOf(label);
						start >= 0;
						start = input.indexOf(label, start + label.length)
					)
						hits.push({ name, label, start });
					return hits;
				}),
		);
	// 长名字优先仅在同一文本位置生效；互不相交的名字仍视为歧义。
	return [
		...new Set(
			candidates
				.filter(
					({ label, start }) =>
						!candidates.some(
							(other) =>
								other.label.length > label.length &&
								other.start <= start &&
								other.start + other.label.length >= start + label.length,
						),
				)
				.map(({ name }) => name),
		),
	];
}

function one<T>(values: readonly T[]): T | undefined {
	return values.length === 1 ? values[0] : undefined;
}

export function resolveTerm(
	value: string,
	terms: readonly TerminologyEntry[],
	categories: readonly TermCategory[],
): string | undefined {
	return one(
		categories.flatMap((category) =>
			matchingNames(value, terms, category, true),
		),
	);
}

function teamMode(input: string): TeamMode | undefined {
	const general = /通用|示例|不按库存/u.test(input);
	const inventory = /按库存|按我的角色|按持有|我的队友|按我的装备/u.test(
		input.replaceAll("不按库存", ""),
	);
	return general === inventory ? undefined : general ? "general" : "inventory";
}

export function parseQuestion(
	prompt: string,
	terms: readonly TerminologyEntry[],
): ScopeDraft {
	const input = normalize(prompt);
	const entityId = one(matchingNames(input, terms, "character", false));
	const versions = [
		...new Set(
			[...input.matchAll(/(?<![\d.])\d{1,3}\.\d{1,3}(?:\.\d{1,3})?(?![\d.])/gu)]
				.filter(
					({ 0: number, index }) =>
						!/^\s*%/u.test(input.slice(index + number.length)) &&
						!/(暴击率|暴伤|暴击伤害|充能效率)\s*[:：=为]?\s*$/u.test(
							input.slice(0, index),
						),
				)
				.map((match) => match[0]),
		),
	];
	const gameVersion = one(versions);
	const goal = /配队|组队|阵容/u.test(input) ? "配队" : undefined;
	const explicitMode = teamMode(input);
	const ambiguousMode =
		!explicitMode && /库存|持有|我的角色|我的队友/u.test(input);
	const mode = explicitMode ?? (goal && !ambiguousMode ? "general" : undefined);
	const equipmentRequired = /按我的装备|限用装备/u.test(input);
	return {
		...(entityId ? { entityId } : {}),
		...(gameVersion ? { gameVersion } : {}),
		...(goal ? { goal } : {}),
		...(mode ? { teamMode: mode } : {}),
		...(equipmentRequired ? { equipmentRequired: true } : {}),
	};
}

type SupplementResult =
	| { readonly patch: ScopeDraft; readonly changes: readonly string[] }
	| undefined;

/** 只接受可明确解释的条件；未知专名、冲突或整个输入未识别时拒绝草稿。 */
export function parseSupplement(
	text: string,
	terms: readonly TerminologyEntry[],
	fields: readonly ScopeField[],
): SupplementResult {
	const parts = text
		.normalize("NFKC")
		.split(/[;；\n]/u)
		.map((part) => part.trim())
		.filter(Boolean);
	let patch: ScopeDraft = {};
	const changes: string[] = [];
	const seen = new Set<ScopeField>();
	for (const part of parts) {
		const [, label, given] =
			/^(角色|版本|目标|模式|目标角色持有|队友|装备)\s*[:：]\s*(.*)$/u.exec(
				part,
			) ?? [];
		const value = given?.trim() ?? part;
		const inferred = !label && fields.length === 1 ? fields[0] : undefined;
		const field =
			(
				{
					角色: "entityId",
					版本: "gameVersion",
					目标: "goal",
					模式: "teamMode",
					目标角色持有: "holding",
					队友: "teammates",
					装备: "equipment",
				} as const
			)[
				label as
					| "角色"
					| "版本"
					| "目标"
					| "模式"
					| "目标角色持有"
					| "队友"
					| "装备"
			] ?? inferred;
		if (!field || !fields.includes(field) || seen.has(field)) return undefined;
		seen.add(field);
		if (field === "entityId") {
			const canonical = one(matchingNames(value, terms, "character", true));
			if (!canonical) return undefined;
			patch = { ...patch, entityId: canonical };
			changes.push(`角色：${canonical}`);
		} else if (field === "gameVersion") {
			if (!versionPattern.test(value)) return undefined;
			patch = { ...patch, gameVersion: value };
			changes.push(`版本：${value}`);
		} else if (field === "goal") {
			if (!/^(配队|组队|阵容)$/u.test(value)) return undefined;
			patch = { ...patch, goal: "配队" };
			changes.push("目标：配队");
		} else if (field === "teamMode") {
			const mode = teamMode(value);
			if (!mode) return undefined;
			patch = { ...patch, teamMode: mode };
			changes.push(`模式：${mode === "inventory" ? "按库存" : "通用"}`);
		} else if (field === "holding") {
			const holding = /^(持有|有)$/u.test(value)
				? "owned"
				: /^(不持有|没有|无)$/u.test(value)
					? "not_owned"
					: undefined;
			if (!holding) return undefined;
			patch = { ...patch, holding };
			changes.push(`目标角色持有：${holding === "owned" ? "持有" : "不持有"}`);
		} else {
			if (!value) return undefined;
			const parts = /^(无|没有|明确为空|\[\])$/u.test(value)
				? []
				: value.split(/[、,，]/u).map((name) => name.trim());
			if (parts.some((name) => !name)) return undefined;
			const canonical = parts.map((name) =>
				one(
					field === "teammates"
						? matchingNames(name, terms, "character", true)
						: [
								...matchingNames(name, terms, "weapon", true),
								...matchingNames(name, terms, "artifact", true),
							],
				),
			);
			if (canonical.some((name) => name === undefined)) return undefined;
			const items = [
				...new Set(
					canonical.filter((name): name is string => name !== undefined),
				),
			];
			patch = { ...patch, [field]: items };
			changes.push(
				`${field === "teammates" ? "队友" : "装备"}：${items.join("、") || "明确为空"}`,
			);
		}
	}
	return changes.length ? { patch, changes } : undefined;
}

export function applySupplement(
	current: ScopeDraft,
	patch: ScopeDraft,
): ScopeDraft {
	if (patch.entityId && patch.entityId !== current.entityId) {
		const { holding: _scopedHolding, ...input } = current;
		return { ...input, ...patch };
	}
	return { ...current, ...patch };
}

export function resolveScope(
	input: ScopeDraft,
	player: PlayerInventory,
): ScopeResolution {
	const { entityId, gameVersion, goal, teamMode } = input;
	if (!entityId || !gameVersion || !goal || !teamMode) {
		const fields: ScopeField[] = [];
		if (!entityId) fields.push("entityId");
		if (!gameVersion) fields.push("gameVersion");
		if (!goal) fields.push("goal");
		if (!teamMode && goal) fields.push("teamMode");
		return { kind: "missing", fields };
	}
	const targetHolding = input.holding ?? player.holdings[entityId] ?? "unknown";
	const teammates = input.teammates ?? player.teammates;
	const equipment = input.equipment;
	if (teamMode === "inventory") {
		if (targetHolding === "not_owned")
			return { kind: "no_team", reason: "target_absent" };
		if (
			teammates?.filter(
				(id) => id !== entityId && player.holdings[id] !== "not_owned",
			).length === 0
		)
			return { kind: "no_team", reason: "no_teammates" };
		const fields: ScopeField[] = [];
		if (targetHolding === "unknown") fields.push("holding");
		if (!teammates) fields.push("teammates");
		if (input.equipmentRequired && !equipment) fields.push("equipment");
		if (fields.length) return { kind: "missing", fields };
	}
	const inventory = (items: readonly string[] | undefined): KnownItems =>
		items ? { kind: "known", items } : { kind: "unknown" };
	return {
		kind: "ready",
		scope: {
			entityId,
			gameVersion,
			goal,
			teamMode,
			guideKind: "team",
			playerConstraints: {
				targetHolding,
				teammates: inventory(
					teammates?.filter(
						(id) => id !== entityId && player.holdings[id] !== "not_owned",
					),
				),
				equipment: inventory(equipment),
			},
		},
	};
}

export function isScopeDraft(value: unknown): value is ScopeDraft {
	if (typeof value !== "object" || value === null || Array.isArray(value))
		return false;
	const input = value as Record<string, unknown>;
	const list = (item: unknown) =>
		Array.isArray(item) &&
		item.every(
			(entry: unknown) => typeof entry === "string" && entry.trim() !== "",
		);
	const text = (item: unknown) =>
		typeof item === "string" && item.trim() !== "";
	return Object.entries(input).every(([field, item]) => {
		switch (field) {
			case "entityId":
			case "goal":
				return text(item);
			case "gameVersion":
				return typeof item === "string" && versionPattern.test(item);
			case "teamMode":
				return item === "general" || item === "inventory";
			case "holding":
				return item === "owned" || item === "not_owned";
			case "teammates":
			case "equipment":
				return list(item);
			case "equipmentRequired":
				return typeof item === "boolean";
			default:
				return false;
		}
	});
}
