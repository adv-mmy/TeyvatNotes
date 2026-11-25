import { IngestError, prepareGuide } from "../../application/ingest-guide.js";
import type {
	FieldProof,
	ParsePage,
} from "../../application/ports/guide-source.js";
import { resolveTerm } from "../../domain/query-scope.js";

const adviceHeading = /^(配队建议|配队推荐|推荐阵容)$/u;
const roleFromTitle = (title: string) =>
	/^(.+?)\s*(?:通用)?配队(?:建议|攻略|推荐)?$/u.exec(title.trim())?.[1];

/** 首批来源规则：单个配队建议章节，字段来自同文档的明确声明。
 * 不识别的布局留待补全，不从问题、搜索摘要或网页发布日期补字段。 */
export const parseWebGuide: ParsePage = (body, url, permission, terms) => {
	const content = body.content.replace(/\r\n?/gu, "\n").trim();
	if (
		!content ||
		content.length > 120_000 ||
		!body.title.trim() ||
		body.title.length > 300 ||
		/验证码|访问受限|访问被拒绝|access denied|captcha|请先登录/i.test(
			`${content} ${body.title}`,
		) ||
		content.includes("\0") ||
		!content.split("\n").some((l) => l.trim() && !/^\s*#|[:：]/u.test(l))
	)
		return { kind: "unreadable" };
	const lines = content.split("\n");
	const proofs: Record<string, FieldProof> = {};
	const fields = new Map<string, string>();
	const reasons = new Set<string>();
	const labels: Record<string, string> = {
		适用游戏版本: "versions",
		适用版本: "versions",
		目标角色: "entity",
		攻略类型: "kind",
		适用条件: "conditions",
		必需角色: "characters",
		必需装备: "equipment",
	};
	let section = "";
	let recommendation = false;
	let hasAdvice = false;
	let pageHeading: string | undefined;
	if (
		/^\s*(?:`{3,}|~{3,}| {4}\S)|<!--|<\/?[a-z][\w-]*(?:\s[^>]*|\/?)>/imu.test(
			content,
		)
	)
		reasons.add("存在未支持的正文布局");
	for (const [i, line] of lines.entries()) {
		const heading = /^(#{1,6})\s+(.+)$/u.exec(line);
		if (heading) {
			section = heading[2] ?? "";
			if (heading[1] === "#") {
				if (pageHeading || recommendation) reasons.add("文档标题关联不明确");
				pageHeading = section;
			} else {
				if (recommendation || !adviceHeading.test(section))
					reasons.add("章节关联不明确");
				recommendation = true;
			}
			continue;
		}
		const cells = line
			.trim()
			.replace(/^\||\|$/gu, "")
			.split("|")
			.map((s) => s.trim());
		const match = /^\s*([^:：]{2,12})[:：]\s*(.+?)\s*$/u.exec(line);
		const label = match?.[1] ?? (cells.length === 2 ? cells[0] : undefined);
		const value = match?.[2] ?? cells[1];
		const key =
			label && Object.hasOwn(labels, label) ? labels[label] : undefined;
		if (key && value) {
			if (fields.has(key) && fields.get(key) !== value)
				reasons.add(`${key} 声明冲突`);
			else {
				fields.set(key, value);
				proofs[key] = {
					location: `${section} · 第 ${i + 1} 行`,
					excerpt: line.slice(0, 300),
				};
			}
		} else if (
			recommendation &&
			adviceHeading.test(section) &&
			!/^\s*(?:!\[|\[.+\]\(|待补充[。.]?$|暂无[。.]?$)/u.test(line) &&
			/[\p{L}\p{N}]{2}/u.test(line)
		)
			hasAdvice = true;
	}
	const titleRole = roleFromTitle(body.title);
	const headingRole = pageHeading && roleFromTitle(pageHeading);
	const titleEntity = titleRole && resolveTerm(titleRole, terms, ["character"]);
	const headingEntity =
		headingRole && resolveTerm(headingRole, terms, ["character"]);
	const entity = fields.get("entity");
	if (
		headingRole &&
		(!headingEntity ||
			(titleEntity && headingEntity !== titleEntity) ||
			(entity && resolveTerm(entity, terms, ["character"]) !== headingEntity))
	)
		reasons.add("正文标题和目标角色不一致");
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
	if (!fields.has("kind") && titleEntity) {
		fields.set("kind", "配队");
		proofs.kind = { location: "title", excerpt: body.title };
	}
	for (const k of ["versions", "entity", "kind", "conditions"])
		if (!fields.has(k)) reasons.add(`缺少 ${k}`);
	if (!hasAdvice) reasons.add("缺少可定位的配队建议");
	const versions = /^([\d.]+)(?:\s*(?:至|~|～)\s*([\d.]+))?$/u.exec(
		fields.get("versions") ?? "",
	);
	const condition = fields.get("conditions") ?? "";
	const split = (s?: string) =>
		s ? s.split(/[、，,；;]/u).map((x) => x.trim()) : [];
	const chars = split(fields.get("characters"));
	const equipment = split(fields.get("equipment"));
	if (condition === "无额外限制" && (chars.length || equipment.length))
		reasons.add("前提矛盾");
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
				else reasons.add("持有前提的专名不明确");
			} else reasons.add("未支持的前提表达");
		}
	}
	if (!reasons.size) {
		try {
			const document = prepareGuide(
				JSON.stringify({
					title: body.title,
					sourceUrl: url,
					entityId: fields.get("entity"),
					guideKind: fields.get("kind") === "配队" ? "team" : undefined,
					gameVersions: {
						from: versions?.[1],
						through: versions?.[2] ?? versions?.[1],
					},
					applicability:
						condition === "无额外限制"
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
				"web-team-1",
			);
			return {
				kind: "ready",
				document: {
					...document,
					// 版本和前提仍可按行回查，但不能把它们单独当作配队建议引用。
					chunks: document.chunks.filter((chunk) =>
						adviceHeading.test(chunk.section.split(" / ").at(-1) ?? ""),
					),
					webEvidence: { parser: "web-team-1", permission, fields: proofs },
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
			url,
			permission,
			proofs,
			reasons: [...reasons],
		},
	};
};
