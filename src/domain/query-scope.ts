export type TermCategory = "character" | "weapon" | "artifact";

export interface TerminologyEntry {
	readonly category: TermCategory;
	readonly name: string;
	readonly aliases: readonly string[];
}

export type TeamMode = "general" | "inventory";
export type Holding = "owned" | "not_owned";
interface InventoryFact {
	readonly category: "character" | "equipment";
	readonly id: string;
	readonly state: Holding | "avoid";
}
export function hasKnownPremise(rule: {
	readonly conditions: readonly string[];
	readonly requiredCharacterIds: readonly string[];
	readonly requiredEquipmentIds: readonly string[];
}): boolean {
	const fields = [
		rule.conditions,
		rule.requiredCharacterIds,
		rule.requiredEquipmentIds,
	];
	return (
		fields.every(
			(values) =>
				Array.isArray(values) &&
				values.every((v) => typeof v === "string" && v.trim()),
		) && fields.some((values) => values.length > 0)
	);
}
export type ScopeField =
	| "entityId"
	| "gameVersion"
	| "goal"
	| "teamMode"
	| "holding"
	| "teammates"
	| "equipment"
	| "inventoryFacts";

export interface ScopeDraft {
	/** null 表示库存声明未识别或相互冲突，必须澄清，不落入通用推荐。 */
	readonly inventoryFacts?: readonly InventoryFact[] | null;
	readonly entityId?: string;
	readonly gameVersion?: string;
	readonly versionIntent?: "latest" | "ambiguous";
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
	readonly equipment?: readonly string[];
	readonly equipmentHoldings?: Readonly<Record<string, Holding>>;
}

type KnownItems =
	| { kind: "unknown" }
	| { kind: "known"; items: readonly string[] };
export type GameVersions = {
	readonly from: string;
	readonly through: string;
} | null;
export type VersionScope =
	| { readonly kind: "exact" | "unknown_reference"; readonly version: string }
	| { readonly kind: "latest"; readonly currentVersion: string | null };

export interface QueryScope {
	readonly entityId: string;
	readonly versionScope: VersionScope;
	readonly guideKind: "team";
	readonly goal: string;
	readonly teamMode: TeamMode;
	readonly playerConstraints: {
		readonly targetHolding: Holding | "unknown";
		readonly teammates: KnownItems;
		readonly equipment: KnownItems;
		readonly excludedCharacters?: readonly string[];
		readonly excludedEquipment?: readonly string[];
		readonly avoidedCharacters?: readonly string[];
		readonly avoidedEquipment?: readonly string[];
		readonly ownedCharacters?: readonly string[];
		readonly ownedEquipment?: readonly string[];
		readonly limitEquipment?: boolean;
		readonly noTeammates?: boolean;
	};
}

export type FrozenQueryScope = Readonly<QueryScope & { scopeId: string }>;

type InventoryRule = {
	readonly requiredCharacterIds: readonly string[];
	readonly requiredEquipmentIds: readonly string[];
};

export type InventoryRejection =
	| "teammates_disallowed"
	| "character_excluded"
	| "target_not_owned"
	| "target_unconfirmed"
	| "teammates_unconfirmed"
	| "character_not_in_confirmed_list"
	| "equipment_excluded"
	| "equipment_list_empty"
	| "equipment_not_in_allowed_list"
	| "equipment_unconfirmed";

/** 只返回固定规则名，保留现有匹配优先级，不输出玩家数据。 */
export function inventoryRejection(
	scope: QueryScope,
	rule: InventoryRule,
): InventoryRejection | undefined {
	const c = scope.playerConstraints;
	if (
		c.noTeammates &&
		rule.requiredCharacterIds.some((id) => id !== scope.entityId)
	)
		return "teammates_disallowed";
	if (
		rule.requiredCharacterIds.some((id) => c.excludedCharacters?.includes(id))
	)
		return "character_excluded";
	if (
		scope.teamMode === "inventory" &&
		c.targetHolding === "not_owned" &&
		rule.requiredCharacterIds.includes(scope.entityId)
	)
		return "target_not_owned";
	if (rule.requiredEquipmentIds.some((id) => c.excludedEquipment?.includes(id)))
		return "equipment_excluded";
	if (
		rule.requiredEquipmentIds.length > 0 &&
		c.equipment.kind === "known" &&
		!c.equipment.items.length
	)
		return "equipment_list_empty";
	if (scope.teamMode === "general") return;
	const teammates = c.teammates;
	for (const id of rule.requiredCharacterIds) {
		if (id === scope.entityId) {
			if (c.targetHolding !== "owned") return "target_unconfirmed";
		} else {
			if (teammates.kind !== "known") {
				if (!c.ownedCharacters?.includes(id)) return "teammates_unconfirmed";
			} else if (!teammates.items.includes(id)) {
				return "character_not_in_confirmed_list";
			}
		}
	}
	const equipment = c.equipment;
	if (equipment.kind === "unknown")
		return rule.requiredEquipmentIds.every((id) =>
			c.ownedEquipment?.includes(id),
		)
			? undefined
			: "equipment_unconfirmed";
	return rule.requiredEquipmentIds.every(
		(id) =>
			equipment.items.includes(id) ||
			(!c.limitEquipment && c.ownedEquipment?.includes(id)),
	)
		? undefined
		: c.limitEquipment
			? "equipment_not_in_allowed_list"
			: "equipment_unconfirmed";
}

/** 库存可用性分类；检索资格与原文是否可引用不使用该分类。 */
export function inventoryFit(
	scope: QueryScope,
	rule: InventoryRule,
): "usable" | "unknown_equipment" | "unavailable" {
	const rejection = inventoryRejection(scope, rule);
	if (!rejection) return "usable";
	return rejection === "equipment_unconfirmed"
		? "unknown_equipment"
		: "unavailable";
}

export function updatedInventory(
	player: PlayerInventory,
	facts: readonly InventoryFact[],
): PlayerInventory {
	const holdings = { ...player.holdings };
	const equipmentHoldings = { ...player.equipmentHoldings };
	for (const { category, id, state } of facts) {
		if (state !== "avoid")
			(category === "character" ? holdings : equipmentHoldings)[id] = state;
	}
	const addOwned = (
		items: readonly string[],
		category: InventoryFact["category"],
	) => [
		...new Set([
			...items,
			...facts
				.filter((f) => f.category === category && f.state === "owned")
				.map((f) => f.id),
		]),
	];
	return {
		...player,
		holdings,
		equipmentHoldings,
		...(player.teammates
			? { teammates: addOwned(player.teammates, "character") }
			: {}),
		...(player.equipment
			? { equipment: addOwned(player.equipment, "equipment") }
			: {}),
	};
}

export function versionApplies(version: string, range: GameVersions): boolean {
	if (range === null) return false;
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

export function requestedGameVersion(scope: VersionScope): string | null {
	return scope.kind === "latest" ? scope.currentVersion : scope.version;
}

export function versionLayers(scope: VersionScope): readonly (string | null)[] {
	const version = requestedGameVersion(scope);
	return scope.kind === "exact" || version === null
		? [version]
		: [version, null];
}

export function matchesVersionLayer(
	layer: string | null,
	range: GameVersions,
): boolean {
	return layer === null ? range === null : versionApplies(layer, range);
}

/** 三段数字比较，7.10 大于 7.2，7.1 等价于 7.1.0。 */
export function gameVersionKey(value: string): number | undefined {
	if (!versionPattern.test(value)) return undefined;
	const [major = 0, minor = 0, patch = 0] = value.split(".").map(Number);
	return (major * 1000 + minor) * 1000 + patch;
}

export type ScopeResolution =
	| { readonly kind: "missing"; readonly fields: readonly ScopeField[] }
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

function splitInventoryClauses(
	prompt: string,
	terms: readonly TerminologyEntry[],
) {
	const questionParts: string[] = [];
	const facts: InventoryFact[] = [];
	let invalidFacts = false;
	const clauses =
		/(?:^|(?:我|本轮|这次)\s*|(?<=\s|配队|组队|阵容)(?=没有|不持有|未持有|没抽到|不能用|不用|不使用|不要用|不想用|别用|不带))(其实|目前|现在|暂时|还|也|可能|大概|似乎|好像|也许)?\s*(没有|不持有|未持有|没抽到|持有|有|不能用|不用|不使用|不要用|不想用|别用|不带)\s*/gu;
	for (const [segment] of normalize(prompt).matchAll(/[^,;。!?\n]+\??/gu)) {
		const part = segment.trim();
		const matches = [...part.matchAll(clauses)];
		const first = matches[0];
		if (!first) {
			if (
				/我(?:到底)?有没有|不确定.{0,6}(?:有没有|是否(?:持|拥)?有|持有|没有)/u.test(
					part,
				)
			)
				invalidFacts = true;
			questionParts.push(part);
			continue;
		}
		questionParts.push(part.slice(0, first.index));
		for (const [i, match] of matches.entries()) {
			let value = part
				.slice(match.index + match[0].length, matches[i + 1]?.index)
				.trim();
			// 也接受先声明库存、再以空格接本轮带明确版本的配队问题。
			const suffix = /\s+(?=\S.*\d{1,3}\.\d{1,3}.*(?:配队|组队|阵容))/u.exec(
				value,
			);
			if (suffix) {
				questionParts.push(value.slice(suffix.index).trim());
				value = value.slice(0, suffix.index);
			}
			if (
				!value ||
				/[?？]$|吗$/u.test(value) ||
				/^(?:可能|大概|似乎|好像|也许)$/u.test(match[1] ?? "") ||
				/(?:如果|假设|假如|要是|是否|不确定)\s*$/u.test(
					part.slice(0, match.index),
				)
			) {
				invalidFacts = true;
				continue;
			}
			const state = /^(?:没有|不持有|未持有|没抽到)$/u.test(match[2] ?? "")
				? "not_owned"
				: /^(?:有|持有)$/u.test(match[2] ?? "")
					? "owned"
					: "avoid";
			for (const entry of value.split("、")) {
				const raw = entry.trim();
				const typed = /^(队友|角色|装备|武器|圣遗物)\s*(.+)$/u.exec(raw);
				const find = (s: string, categories: readonly TermCategory[]) =>
					categories.flatMap((cat) =>
						matchingNames(s, terms, cat, true).map((id) => ({
							category:
								cat === "character"
									? ("character" as const)
									: ("equipment" as const),
							id,
							state,
						})),
					);
				const candidates = find(raw, ["character", "weapon", "artifact"]);
				const fact = one(
					candidates.length || !typed
						? candidates
						: find(
								typed[2] ?? "",
								/队友|角色/u.test(typed[1] ?? "")
									? ["character"]
									: typed[1] === "武器"
										? ["weapon"]
										: typed[1] === "圣遗物"
											? ["artifact"]
											: ["weapon", "artifact"],
							),
				);
				if (
					!fact ||
					facts.some(
						(f) =>
							f.category === fact.category &&
							f.id === fact.id &&
							f.state !== "avoid" &&
							state !== "avoid" &&
							f.state !== state,
					)
				)
					invalidFacts = true;
				else facts.push({ ...fact, state });
			}
		}
	}
	return {
		input: questionParts.join(" ").trim(),
		facts: invalidFacts ? null : facts,
	};
}

export function parseQuestion(
	prompt: string,
	terms: readonly TerminologyEntry[],
): ScopeDraft {
	const { input, facts } = splitInventoryClauses(prompt, terms);
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
	const versionIntent =
		versions.length > 1 ||
		(!gameVersion && /上(?:一|个|一个)?版本|旧版本/u.test(input))
			? "ambiguous"
			: !gameVersion && /最新(?:版|版本)?|当前版本/u.test(input)
				? "latest"
				: undefined;
	const goal = /配队|组队|阵容/u.test(input) ? "配队" : undefined;
	const explicitMode = teamMode(input);
	const ambiguousMode =
		!explicitMode && /库存|持有|我的角色|我的队友/u.test(input);
	const mode = explicitMode ?? (goal && !ambiguousMode ? "general" : undefined);
	const equipmentRequired = /按我的装备|限用装备/u.test(input);
	return {
		...(facts === null
			? { inventoryFacts: null }
			: facts.length
				? { inventoryFacts: facts }
				: {}),
		...(entityId ? { entityId } : {}),
		...(gameVersion ? { gameVersion } : {}),
		...(versionIntent ? { versionIntent } : {}),
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
			/^(角色|版本|目标|模式|目标角色持有|队友|装备|库存约束)\s*[:：]\s*(.*)$/u.exec(
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
					库存约束: "inventoryFacts",
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
					| "库存约束"
			] ?? inferred;
		if (!field || !fields.includes(field) || seen.has(field)) return undefined;
		seen.add(field);
		if (field === "entityId") {
			const canonical = one(matchingNames(value, terms, "character", true));
			if (!canonical) return undefined;
			patch = { ...patch, entityId: canonical };
			changes.push(`角色：${canonical}`);
		} else if (field === "gameVersion") {
			if (versionPattern.test(value)) patch = { ...patch, gameVersion: value };
			else if (/^(?:最新(?:版|版本)?|当前版本)$/u.test(value))
				patch = { ...patch, versionIntent: "latest" };
			else return undefined;
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
		} else if (field === "inventoryFacts") {
			const { facts, input } = splitInventoryClauses(value, terms);
			if (!facts || (value !== "无" && (input || !facts.length)))
				return undefined;
			patch = { ...patch, inventoryFacts: facts };
			changes.push(
				`库存约束：${facts.map((f) => (f.state === "avoid" ? "本轮不用" : f.state === "owned" ? "持有" : "未持有") + f.id).join("、") || "无"}`,
			);
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
	if (patch.gameVersion || patch.versionIntent) {
		const { gameVersion: _version, versionIntent: _intent, ...rest } = current;
		current = rest;
	}
	// 补答一件装备不能抹去原问题的“本轮不用某队友”；只替换被纠正的那项事实。
	if (current.inventoryFacts?.length) {
		const keep = current.inventoryFacts.filter((f) => {
			if (
				f.state !== "avoid" &&
				(f.category === "equipment"
					? patch.equipment?.length === 0 || patch.equipment?.includes(f.id)
					: patch.teammates?.includes(f.id) ||
						(patch.holding && f.id === (patch.entityId ?? current.entityId)))
			)
				return false;
			return !patch.inventoryFacts?.some(
				(n) =>
					n.category === f.category &&
					n.id === f.id &&
					(n.state === "avoid") === (f.state === "avoid"),
			);
		});
		patch = {
			...patch,
			inventoryFacts: [...keep, ...(patch.inventoryFacts ?? [])],
		};
	}
	if (patch.entityId && patch.entityId !== current.entityId) {
		const { holding: _scopedHolding, ...input } = current;
		return { ...input, ...patch };
	}
	return { ...current, ...patch };
}

export function resolveScope(
	input: ScopeDraft,
	player: PlayerInventory,
	currentVersion: string | null = null,
): ScopeResolution {
	const { entityId, gameVersion, goal, teamMode } = input;
	const badVersion =
		input.versionIntent === "ambiguous" ||
		(gameVersion !== undefined && gameVersionKey(gameVersion) === undefined);
	if (
		!entityId ||
		badVersion ||
		!goal ||
		!teamMode ||
		input.inventoryFacts === null
	) {
		const fields: ScopeField[] = [];
		if (!entityId) fields.push("entityId");
		if (badVersion) fields.push("gameVersion");
		if (!goal) fields.push("goal");
		if (!teamMode && goal) fields.push("teamMode");
		if (input.inventoryFacts === null) fields.push("inventoryFacts");
		return { kind: "missing", fields };
	}
	const facts = input.inventoryFacts ?? [];
	player = updatedInventory(player, facts);
	const excluded = (
		category: InventoryFact["category"],
		holdings: Readonly<Record<string, Holding>>,
	) => [
		...new Set([
			...Object.keys(holdings).filter((id) => holdings[id] === "not_owned"),
			...facts
				.filter((f) => f.category === category && f.state === "avoid")
				.map((f) => f.id),
		]),
	];
	const excludedCharacters = excluded("character", player.holdings);
	const excludedEquipment = excluded(
		"equipment",
		player.equipmentHoldings ?? {},
	);
	const avoided = (category: InventoryFact["category"]) => [
		...new Set(
			facts
				.filter((f) => f.category === category && f.state === "avoid")
				.map((f) => f.id),
		),
	];
	const avoidedCharacters = avoided("character");
	const avoidedEquipment = avoided("equipment");
	const ownedCharacters = Object.entries(player.holdings)
		.filter(([id, value]) => value === "owned" && id !== entityId)
		.map(([id]) => id);
	const ownedEquipment = Object.entries(player.equipmentHoldings ?? {})
		.filter(([, v]) => v === "owned")
		.map(([id]) => id);
	const targetHolding = input.holding ?? player.holdings[entityId] ?? "unknown";
	const teammates = input.teammates ?? player.teammates;
	const equipment = input.equipment ?? player.equipment;
	const inventory = (items: readonly string[] | undefined): KnownItems =>
		items ? { kind: "known", items } : { kind: "unknown" };
	return {
		kind: "ready",
		scope: {
			entityId,
			versionScope: gameVersion
				? { kind: "exact", version: gameVersion }
				: { kind: "latest", currentVersion },
			goal,
			teamMode,
			guideKind: "team",
			playerConstraints: {
				...(teammates?.length === 0 ? { noTeammates: true } : {}),
				...(input.equipmentRequired ? { limitEquipment: true } : {}),
				...(excludedCharacters.length ? { excludedCharacters } : {}),
				...(excludedEquipment.length ? { excludedEquipment } : {}),
				...(avoidedCharacters.length ? { avoidedCharacters } : {}),
				...(avoidedEquipment.length ? { avoidedEquipment } : {}),
				...(ownedCharacters.length ? { ownedCharacters } : {}),
				...(ownedEquipment.length ? { ownedEquipment } : {}),
				targetHolding,
				teammates: inventory(
					teammates?.filter(
						(id) => id !== entityId && !excludedCharacters.includes(id),
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
			case "versionIntent":
				return item === "latest" || item === "ambiguous";
			case "teamMode":
				return item === "general" || item === "inventory";
			case "holding":
				return item === "owned" || item === "not_owned";
			case "teammates":
			case "equipment":
				return list(item);
			case "equipmentRequired":
				return typeof item === "boolean";
			case "inventoryFacts":
				return (
					item === null ||
					(Array.isArray(item) &&
						item.every(
							(f) =>
								f &&
								(f.category === "character" || f.category === "equipment") &&
								text(f.id) &&
								["owned", "not_owned", "avoid"].includes(f.state),
						))
				);
			default:
				return false;
		}
	});
}
