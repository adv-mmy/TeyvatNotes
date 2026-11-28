import { IngestError, prepareGuide } from "../../application/ingest-guide.js";
import type {
	FieldProof,
	ParsePage,
} from "../../application/ports/guide-source.js";
import { readExplicitTeam } from "../../domain/answer-validation.js";
import { resolveTerm } from "../../domain/query-scope.js";
import { wikiHeading } from "./bwiki-source.js";

const roleFromTitle = (title: string) =>
	/^([^/]+)\/攻略$/u.exec(title.trim())?.[1];
const parserVersion = "bwiki-team-4";
const layoutLabel = /^[\p{L}\p{M}\p{N} _·—-]{1,60}$/u;

/** 保留已知模板原文中可核对的参数，不展开任何模板。 */
function projectInline(
	raw: string,
	terms: Parameters<ParsePage>[3],
): string | undefined {
	if (raw.includes("{{{")) return;
	let line = raw;
	for (let depth = 0; line.includes("{{") && depth < 12; depth++) {
		let unsupported = false;
		const next = line.replace(
			/\{\{([^{}\n]+)\}\}/gu,
			(_full, value: string) => {
				const [name, ...args] = value.split("|").map((arg) => arg.trim());
				if (name === "图标/攻略" || name === "图标") {
					const role =
						args.length === 1
							? args[0]
							: args.length === 2 && args[0] === "角色"
								? args[1]
								: undefined;
					const canonical = role && resolveTerm(role, terms, ["character"]);
					if (canonical) return canonical;
				}
				if (
					name === "颜色" &&
					args.length === 2 &&
					args[0] &&
					layoutLabel.test(args[0]) &&
					args[1]
				)
					return args[1];
				unsupported = true;
				return "";
			},
		);
		if (unsupported || next === line) return;
		line = next;
	}
	line = line
		.replace(
			/\[\[([^[[\]\n|:]+)(?:\|([^[[\]\n|]+))?\]\]/gu,
			(_link, target: string, label?: string) => label ?? target,
		)
		.replace(/'{2,5}/gu, "")
		.trim();
	return /[[\]{}]/u.test(line) || /^(?:={1,6}\s*$|https?:\/\/)/iu.test(line)
		? undefined
		: line;
}

/** 只识别容器的尺寸、边距及滚动设置；样式不会进入正文或被执行。 */
function containerStyle(style: string): boolean {
	if (!style.trim() || style.length > 480) return false;
	const items = style.trim().replace(/;$/u, "").split(";");
	const seen = new Set<string>();
	for (const item of items) {
		const declaration = /^\s*([a-z-]+)\s*:\s*([a-z0-9.%\t ]+?)\s*$/iu.exec(
			item,
		);
		if (!declaration?.[1] || !declaration[2]) return false;
		const name = declaration[1].toLowerCase();
		const value = declaration[2].toLowerCase();
		if (seen.has(name)) return false;
		seen.add(name);
		if (/^(?:(?:min|max)-)?(?:width|height)$/u.test(name)) {
			if (!/^(?:auto|\d{1,4}(?:\.\d{1,3})?(?:px|em|rem|%))$/u.test(value))
				return false;
		} else if (
			/^(?:margin|padding)(?:-(?:top|right|bottom|left))?$/u.test(name)
		) {
			const sizes = value.split(/[ \t]+/u);
			if (sizes.length > (name.includes("-") ? 1 : 4)) return false;
			if (
				!sizes.every(
					(n) =>
						/^(?:0|\d{1,4}(?:\.\d{1,3})?(?:px|em|rem|%))$/u.test(n) ||
						(name.startsWith("margin") && n === "auto"),
				)
			)
				return false;
		} else if (/^overflow(?:-[xy])?$/u.test(name)) {
			if (!/^(?:auto|visible|scroll)$/u.test(value)) return false;
		} else return false;
	}
	return true;
}

function containerAttributes(attrs: string): boolean {
	const seen = new Set<string>();
	// 按完整属性读取，不删除或容忍未知内容。
	for (const [, attr, value] of attrs.matchAll(
		/\s+([a-z]+)="([^"]*)"|\S+/giu,
	)) {
		const name = attr?.toLowerCase();
		if (!name || value === undefined || seen.has(name)) return false;
		seen.add(name);
		if (
			name === "style"
				? !containerStyle(value)
				: !["class", "id"].includes(name) ||
					!/^[\p{L}\p{N} _-]{1,120}$/u.test(value)
		)
			return false;
	}
	return true;
}

/** 行内粗体保留连续文字；容器及换行仍分隔证据，不跨区拼接。 */
function stripLayout(raw: string, opened: string[]): string | undefined {
	let invalid = false;
	const stripped = raw.replace(
		/<(\/?)([a-z]+)\b([^<>]*)>/giu,
		(_full, close: string, tag: string, attrs: string) => {
			const name = tag.toLowerCase();
			const inline = name === "b" || name === "strong";
			if (name === "br" && !close && /^\s*\/?\s*$/u.test(attrs)) return "\t";
			if (!inline && !["div", "center", "tabber"].includes(name))
				invalid = true;
			else if (close) {
				if (attrs.trim() || opened.pop() !== name) invalid = true;
			} else if (name === "div" ? containerAttributes(attrs) : !attrs.trim()) {
				opened.push(name);
				if (opened.length > 12) invalid = true;
			} else invalid = true;
			return inline ? "" : "\t";
		},
	);
	return invalid || /[<>]/u.test(stripped) ? undefined : stripped;
}

interface PanelBoard {
	readonly panels: { title: string; defaultVisible: boolean }[];
	next: number;
	active?: string;
}

/** 按源文的物理行投影；独立页签和换行不共享阵容片段。 */
function readableLines(
	content: string,
	terms: Parameters<ParsePage>[3],
): string[][] | undefined {
	const masked = content.replace(/<!--[\s\S]*?-->/gu, (part) =>
		part.replace(/[^\n]/gu, " "),
	);
	let templateDepth = 0;
	let tableStart: number | undefined;
	const opened: string[] = [];
	let board: PanelBoard | undefined;
	const lines: string[][] = [];
	for (const raw of masked.split("\n")) {
		const inTabber = opened.includes("tabber");
		const layout = stripLayout(raw.replaceAll("\t", " "), opened);
		if (layout === undefined) return;
		const parts = layout
			.split("\t")
			.map((value) => value.trim())
			.filter(Boolean);
		const [single] = parts.length === 1 ? parts : [];
		const switcher =
			single && templateDepth === 0 && tableStart === undefined
				? /^\{\{\s*切换板\s*\|\s*(开始|结束|默认显示|默认折叠|显示内容|折叠内容|内容结束)\s*(?:\|\s*([^|{}]+?)\s*)?\}\}$/u.exec(
						single,
					)
				: undefined;
		if (switcher) {
			const [, command, label] = switcher;
			if (command === "开始") {
				if (board || label) return;
				board = { panels: [], next: 0 };
			} else if (!board) return;
			else if (command === "默认显示" || command === "默认折叠") {
				if (
					!label ||
					!layoutLabel.test(label) ||
					board.next ||
					board.panels.some((panel) => panel.title === label) ||
					board.panels.length >= 16
				)
					return;
				board.panels.push({
					title: label,
					defaultVisible: command === "默认显示",
				});
			} else if (command === "显示内容" || command === "折叠内容") {
				const expected = board.panels[board.next];
				if (
					label ||
					board.active ||
					!expected ||
					expected.defaultVisible !== (command === "显示内容")
				)
					return;
				board.active = expected.title;
				board.next++;
				lines.push([`=== ${expected.title} ===`]);
				continue;
			} else if (command === "内容结束") {
				if (label || !board.active) return;
				delete board.active;
			} else {
				if (
					label ||
					!board.next ||
					board.active ||
					board.next !== board.panels.length
				)
					return;
				board = undefined;
			}
			lines.push([]);
			continue;
		}
		if (board && !board.active && parts.length) return;
		const tab =
			templateDepth === 0 && (inTabber || opened.includes("tabber")) && single
				? (tableStart === undefined
						? /^(?:\|-\|)?\s*(.+?)\s*=\s*$/u
						: /^\|-\|\s*(.+?)\s*=\s*$/u
					).exec(single)?.[1]
				: undefined;
		if (tab && layoutLabel.test(tab)) {
			if (tableStart !== undefined) {
				// Tabber 的显式切页独立渲染。前一页没闭合的表格不作为证据。
				for (let i = tableStart; i < lines.length; i++) lines[i] = [];
				tableStart = undefined;
			}
			lines.push([`==== ${tab} ====`]);
			continue;
		}
		if (inTabber && !opened.includes("tabber") && !parts.length) {
			if (tableStart !== undefined) return;
			lines.push([`=== ${board?.active ?? "阵容详情"} ===`]);
			continue;
		}
		const beganInTemplate = templateDepth > 0;
		for (const marker of raw.match(/\{\{|\}\}/gu) ?? []) {
			templateDepth += marker === "{{" ? 1 : -1;
			if (templateDepth < 0) return;
		}
		if (beganInTemplate || templateDepth > 0) {
			lines.push([]);
			continue;
		}
		const row = parts[0] ?? "";
		if (row.startsWith("{|") || row.startsWith("|}")) {
			const opening = row.startsWith("{|");
			if (opening === (tableStart !== undefined)) return;
			tableStart = opening ? lines.length : undefined;
			lines.push([]);
			continue;
		}
		const projected = parts
			.map((part) => projectInline(part, terms))
			.filter((part): part is string => !!part);
		if (tableStart === undefined) {
			lines.push(projected);
			continue;
		}
		// 两列且有明确标签才视为同一声明。其他可读单元格逐个保留，
		// 不把分栏或跨行单元格拼成阵容。
		const cells =
			parts.length === 1 && projected[0]
				? /^\|\s*([^|]+?)\s*\|\|\s*([^|]+?)\s*$/u.exec(projected[0])
				: undefined;
		lines.push(
			cells
				? [`${cells[1]}：${cells[2]}`]
				: /^[|!](?!-)/u.test(row)
					? parts
							.flatMap((part) =>
								part.replace(/^[|!]\s*/u, "").split(/\|\||!!/u),
							)
							.map((part) =>
								projectInline(
									part.replace(
										/^(?:(?:rowspan|colspan|style|class)="[^"]*"\s*)+\|\s*/iu,
										"",
									),
									terms,
								),
							)
							.filter((part): part is string => !!part && !part.includes("|"))
					: [],
		);
	}
	return templateDepth === 0 &&
		tableStart === undefined &&
		!board &&
		!opened.length
		? lines
		: undefined;
}

/** 发布可追溯的正文；阵容、适用条件可独立提取，但不决定能否入库。 */
export const parseWebGuide: ParsePage = (body, url, permission, terms) => {
	const content = body.content
		.replace(/^\uFEFF/u, "")
		.replace(/\r\n?/gu, "\n")
		.replace(/^\n+|\s+$/gu, "");
	if (
		!content ||
		content.length > 120_000 ||
		!body.title.trim() ||
		body.title.length > 300 ||
		/验证码|访问受限|访问被拒绝|access denied|captcha|请先登录/i.test(
			`${content} ${body.title}`,
		) ||
		/\p{Cc}/u.test(content.replace(/[\n\t]/gu, ""))
	)
		return { kind: "unreadable" };
	const rawLines = content.split("\n");
	const lines = readableLines(content, terms);
	if (!lines) return { kind: "unreadable" };
	const proofs: Record<string, FieldProof> = {};
	const fields = new Map<string, string>();
	const reasons = new Set<string>();
	const optional = new Set(["conditions", "characters", "equipment"]);
	const premiseLines: { line: number; text: string }[] = [];
	let premiseUnknown = false;
	const labels: Record<string, string> = {
		适用游戏版本: "versions",
		适用版本: "versions",
		目标角色: "entity",
		攻略类型: "kind",
		适用条件: "conditions",
		必需角色: "characters",
		必需装备: "equipment",
	};
	let section = body.revision.section.title;
	const first = lines[0];
	const root = wikiHeading(first?.length === 1 ? (first[0] ?? "") : "");
	if (!root || root.title !== section) reasons.add("存在未支持的正文布局");
	const rendered = rawLines.map(() => "");
	const rosters: {
		line: number;
		section: string;
		body: string;
		team: readonly string[];
		equipmentNote?: string;
	}[] = [];
	let prose = false;
	let subsection = false;
	function keep(i: number, line: string) {
		rendered[i] = rendered[i] ? `${rendered[i]} ｜ ${line}` : line;
	}
	for (const [i, parts] of lines.entries()) {
		const heading =
			parts.length === 1 ? wikiHeading(parts[0] ?? "") : undefined;
		if (heading) {
			section = heading.title;
			if (i > 0) {
				subsection = true;
				if (heading.depth <= (root?.depth ?? 0)) reasons.add("章节关联不明确");
			}
			rendered[i] =
				`${"#".repeat(Math.max(1, heading.depth - (root?.depth ?? 0) + 1))} ${section}`;
			continue;
		}
		for (const line of parts) {
			const match = /^(?:[*#;:]\s*)?([^:：]{2,12})[:：]\s*(.+?)\s*$/u.exec(
				line,
			);
			const label = match?.[1]?.trim();
			const value = match?.[2];
			const key =
				label && Object.hasOwn(labels, label) ? labels[label] : undefined;
			if (key && value) {
				const isPremise = optional.has(key);
				if (isPremise) premiseLines.push({ line: i, text: line });
				if (isPremise && subsection) {
					premiseUnknown = true;
					continue;
				}
				if (fields.has(key) && fields.get(key) !== value) {
					if (isPremise) premiseUnknown = true;
					else reasons.add(`${key} 声明冲突`);
				} else {
					fields.set(key, value);
					proofs[key] = {
						location: `${section} · 第 ${i + 1} 行`,
						excerpt: (rawLines[i] ?? line).slice(0, 300),
					};
				}
			} else if (
				!/^(?:#{1,6}\s|待补充[。.]?$|暂无[。.]?$|\*?\s*#redirect\b)/iu.test(
					line,
				) &&
				/[\p{L}\p{N}]{2}/u.test(line)
			) {
				prose = true;
				keep(i, line);
				const lineup = parts.length === 1 && readExplicitTeam(line, terms);
				if (lineup) rosters.push({ line: i, section, body: line, ...lineup });
			}
		}
	}
	if (!prose) return { kind: "unreadable" };
	const titleRole = roleFromTitle(body.title);
	const titleEntity = titleRole && resolveTerm(titleRole, terms, ["character"]);
	const entity = fields.get("entity");
	if (titleRole && !titleEntity) reasons.add("标题角色不明确");
	if (
		titleEntity &&
		entity &&
		resolveTerm(entity, terms, ["character"]) !== titleEntity
	)
		reasons.add("标题和目标角色冲突");
	if (!entity && titleEntity) {
		fields.set("entity", titleEntity);
		proofs.entity = { location: "title", excerpt: body.title };
	}
	if (!fields.has("kind") && root?.title === body.revision.section.title) {
		fields.set("kind", "配队");
		proofs.kind = {
			location: `${root.title} · 第 1 行`,
			excerpt: rawLines[0] ?? "",
		};
	}
	for (const k of ["entity", "kind"])
		if (!fields.has(k)) reasons.add(`缺少 ${k}`);
	const versionText = fields.get("versions");
	const unknownVersion =
		!versionText || /^(?:未知|不明|未注明)$/u.test(versionText);
	const versions = /^([\d.]+)(?:\s*(?:至|~|～)\s*([\d.]+))?$/u.exec(
		versionText ?? "",
	);
	if (!unknownVersion && !versions) reasons.add("适用版本声明无法核对");
	const condition = fields.get("conditions") ?? "";
	if (!condition) premiseUnknown = true;
	const split = (s?: string) =>
		s ? s.split(/[、，,；;]/u).map((x) => x.trim()) : [];
	const chars: string[] = [];
	const equipment: string[] = [];
	for (const item of split(fields.get("characters"))) {
		const role = resolveTerm(item, terms, ["character"]);
		if (role) chars.push(role);
		else premiseUnknown = true;
	}
	for (const item of split(fields.get("equipment"))) {
		const gear = resolveTerm(item, terms, ["weapon", "artifact"]);
		if (gear) equipment.push(gear);
		else premiseUnknown = true;
	}
	if (condition === "无额外限制" && (chars.length || equipment.length))
		premiseUnknown = true;
	if (condition !== "无额外限制") {
		for (const part of split(condition)) {
			const holding = /^(?:需(?:要)?持有|持有|必须装备|需要装备)\s*(.+)$/u.exec(
				part,
			)?.[1];
			if (holding) {
				const character =
					resolveTerm(holding, terms, ["character"]) ??
					resolveTerm(holding.replace(/^角色\s*/u, ""), terms, ["character"]);
				const gear =
					resolveTerm(holding, terms, ["weapon", "artifact"]) ??
					resolveTerm(holding.replace(/^装备\s*/u, ""), terms, [
						"weapon",
						"artifact",
					]);
				if (character && !gear) chars.push(character);
				else if (gear && !character) equipment.push(gear);
				else premiseUnknown = true;
			} else premiseUnknown = true;
		}
	}
	if (premiseUnknown) {
		for (const { line, text } of premiseLines) keep(line, text);
	}
	const target = resolveTerm(fields.get("entity") ?? "", terms, ["character"]);
	const requiredEquipment = new Set(
		equipment.map((item) => resolveTerm(item, terms, ["weapon", "artifact"])),
	);
	const verified = rosters.filter(({ team, equipmentNote }) => {
		if (!target || !team.includes(target)) return false;
		if (!equipmentNote) return true;
		// 句内装备说明只能重述已核对的前提，不能悄悄丢掉另一套条件。
		const [, wearer, item] = /^为(.+?)装备(.+)$/u.exec(equipmentNote) ?? [];
		const character = wearer && resolveTerm(wearer, terms, ["character"]);
		const gear = item && resolveTerm(item, terms, ["weapon", "artifact"]);
		return (
			!!character &&
			team.includes(character) &&
			!!gear &&
			requiredEquipment.has(gear)
		);
	});
	const firstRoster = verified[0];
	if (firstRoster)
		proofs.roster = {
			location: `${firstRoster.section} · 第 ${firstRoster.line + 1} 行`,
			excerpt: (rawLines[firstRoster.line] ?? firstRoster.body).slice(0, 300),
		};
	if (!reasons.size) {
		try {
			const document = prepareGuide(
				JSON.stringify({
					title: body.title,
					sourceUrl: url,
					entityId: fields.get("entity"),
					guideKind: fields.get("kind") === "配队" ? "team" : undefined,
					gameVersions: unknownVersion
						? null
						: {
								from: versions?.[1],
								through: versions?.[2] ?? versions?.[1],
							},
					applicability: premiseUnknown
						? { kind: "unknown" }
						: condition === "无额外限制"
							? { kind: "unrestricted" }
							: {
									kind: "known",
									conditions: [condition],
									requiredCharacterIds: chars,
									requiredEquipmentIds: equipment,
								},
					body: content,
				}),
				"json",
				terms,
				parserVersion,
				{
					projectBody: () => rendered.join("\n"),
					allowUnknownVersion: true,
					allowUnknownApplicability: true,
				},
			);
			return {
				kind: "ready",
				document: {
					...document,
					webEvidence: {
						parser: parserVersion,
						permission,
						fields: proofs,
						revision: body.revision,
						...(unknownVersion
							? { unknownGameVersionReason: "not_stated" as const }
							: {}),
					},
				},
			};
		} catch (error) {
			if (!(error instanceof IngestError)) throw error;
			reasons.add(error.code);
		}
	}
	return {
		kind: "incomplete",
		page: {
			title: body.title,
			content,
			revision: body.revision,
			url,
			permission,
			proofs,
			reasons: [...reasons],
		},
	};
};
