import { createHash } from "node:crypto";
import {
	gameVersionKey,
	hasKnownPremise,
	resolveTerm,
	type TermCategory,
	type TerminologyEntry,
	versionApplies,
} from "../domain/query-scope.js";
import { TurnToolError } from "./guide-tools.js";
import { untilAborted } from "./ports/agent.js";
import type {
	EmbeddingConfig,
	EmbeddingPort,
	GuideWriteRepository,
	ImportDocument,
	ImportedChunk,
	StoredGuide,
	VectorWritePort,
} from "./ports/guide-data.js";
import {
	type GuideSource,
	type IncompletePage,
	type ParsePage,
	permittedUrl,
	type SourcePermit,
	type SupplementStatus,
	validPermit,
	type WebSupplement,
} from "./ports/guide-source.js";

export type IngestErrorCode =
	| "GUIDE_INVALID"
	| "GUIDE_METADATA_INCOMPLETE"
	| "GUIDE_NOT_FOUND"
	| "INDEX_NOT_READY"
	| "GUIDE_STORAGE_UNAVAILABLE";

export class IngestError extends Error {
	constructor(readonly code: IngestErrorCode) {
		super(code);
	}
}

const manualProcessingVersion = "team-1";
const maxBody = 120_000;
const maxChunk = 2400;
const sha = (text: string) => createHash("sha256").update(text).digest("hex");
const canonical = (text: string) => text.normalize("NFKC").trim().toLowerCase();
const object = (value: unknown): value is Record<string, unknown> =>
	typeof value === "object" && value !== null && !Array.isArray(value);
const string = (value: unknown) =>
	typeof value === "string" && !value.includes("\0") && value.trim()
		? value.trim()
		: undefined;

function invalid(): never {
	throw new IngestError("GUIDE_INVALID");
}
function incomplete(): never {
	throw new IngestError("GUIDE_METADATA_INCOMPLETE");
}

function sourceUrl(value: unknown): string {
	const raw = string(value) ?? incomplete();
	let url: URL;
	try {
		url = new URL(raw);
	} catch {
		return invalid();
	}
	if (
		url.protocol !== "https:" ||
		url.username ||
		url.password ||
		raw.length > 2048
	)
		return invalid();
	url.hash = "";
	return url.href;
}

function list(value: unknown): string[] {
	if (!Array.isArray(value) || value.length > 32) return incomplete();
	return [
		...new Set(
			value.map((item: unknown) => {
				const text = string(item);
				if (!text || text.length > 300) return incomplete();
				return text;
			}),
		),
	].sort();
}

function requiredTerm(
	value: unknown,
	terms: readonly TerminologyEntry[],
	categories: readonly TermCategory[],
): string {
	const text = string(value) ?? incomplete();
	return resolveTerm(text, terms, categories) ?? incomplete();
}

function parseMarkdown(raw: string): Record<string, unknown> {
	const lines = raw.split("\n");
	const split = lines.findIndex((line) => line.trim() === "---");
	const title = /^#\s+(.+)$/u.exec(lines[0] ?? "")?.[1];
	if (!title || split < 2 || split > 20) return incomplete();
	const allowed = new Set([
		"来源",
		"角色",
		"类型",
		"适用版本",
		"适用条件",
		"必需角色",
		"必需装备",
	]);
	const meta = new Map<string, string>();
	for (const line of lines.slice(1, split)) {
		if (!line.trim()) continue;
		const match = /^\s*([^:：]+)[:：]\s*(.*?)\s*$/u.exec(line);
		const key = match?.[1]?.trim();
		if (!key || !allowed.has(key) || meta.has(key)) return incomplete();
		meta.set(key, match?.[2] ?? "");
	}
	const versions = /^([\d.]+)(?:\s*(?:至|~|～)\s*([\d.]+))?$/u.exec(
		meta.get("适用版本") ?? "",
	);
	const conditions = meta.get("适用条件");
	const csv = (value?: string) =>
		value ? value.split(/[、，,；;]/u).map((item) => item.trim()) : [];
	const chars = csv(meta.get("必需角色"));
	const equipment = csv(meta.get("必需装备"));
	return {
		title,
		sourceUrl: meta.get("来源"),
		entityId: meta.get("角色"),
		guideKind: meta.get("类型") === "配队" ? "team" : undefined,
		gameVersions: {
			from: versions?.[1],
			through: versions?.[2] ?? versions?.[1],
		},
		applicability:
			conditions === "无额外限制"
				? chars.length || equipment.length
					? undefined
					: { kind: "unrestricted" }
				: conditions && conditions !== "未知"
					? {
							kind: "known",
							conditions: csv(conditions),
							requiredCharacterIds: chars,
							requiredEquipmentIds: equipment,
						}
					: undefined,
		body: lines.slice(split + 1).join("\n"),
		bodyOffset: split + 1,
	};
}

function chunksFor(
	content: string,
	lineOffset: number,
	id: string,
	entity: string,
	terms: readonly TerminologyEntry[],
): ImportedChunk[] {
	const chunks: ImportedChunk[] = [];
	const lines = content.split("\n");
	const headings: { depth: number; label: string }[] = [];
	let start = -1;
	let end = 0;
	let blockStart = -1;
	let fence: string | undefined;
	const termsByLabel = terms.map(({ name, aliases }) =>
		[name, ...aliases].map(canonical),
	);
	const targetLabels = terms.find(
		({ name, category }) => name === entity && category === "character",
	);
	function flush() {
		if (start < 0) return;
		const body = lines.slice(start, end).join("\n");
		const section = headings.map(({ label }) => label).join(" / ") || "正文";
		const text = canonical(`${section}\n${body}`);
		const keywords = termsByLabel.flatMap((labels) =>
			labels.some((label) => label && text.includes(label)) ? labels : [],
		);
		chunks.push({
			id: sha(`${id}:${chunks.length + 1}`),
			section,
			location: `${section} · 第 ${start + lineOffset + 1}–${end + lineOffset} 行`,
			body,
			terms: [
				...new Set([
					...keywords,
					...[entity, ...(targetLabels?.aliases ?? [])].map(canonical),
				]),
			].filter(Boolean),
		});
		start = -1;
	}
	function addBlock(to: number) {
		if (blockStart < 0) return;
		if (lines.slice(blockStart, to).join("\n").length > maxChunk)
			return invalid();
		if (start >= 0 && lines.slice(start, to).join("\n").length > maxChunk)
			flush();
		if (start < 0) start = blockStart;
		end = to;
		blockStart = -1;
	}
	for (let i = 0; i < lines.length; i++) {
		const line = lines[i] ?? "";
		const marker = /^\s*(`{3,}|~{3,})/u.exec(line)?.[1];
		if (fence) {
			if (marker?.startsWith(fence)) fence = undefined;
		} else if (marker) {
			fence = marker;
			if (blockStart < 0) blockStart = i;
		} else {
			const heading = /^(#{1,6})\s+(.+)$/u.exec(line);
			if (heading?.[1] && heading[2]) {
				addBlock(i);
				flush();
				const depth = heading[1].length;
				while ((headings.at(-1)?.depth ?? 0) >= depth) headings.pop();
				headings.push({ depth, label: heading[2].trim() });
			} else if (!line.trim()) {
				addBlock(i);
			} else if (blockStart < 0) blockStart = i;
		}
	}
	if (fence) return invalid();
	addBlock(lines.length);
	flush();
	if (!chunks.length || chunks.length > 128) return invalid();
	return chunks;
}

export function prepareGuide(
	raw: string,
	format: "json" | "markdown",
	terms: readonly TerminologyEntry[],
	processingVersion = manualProcessingVersion,
): ImportDocument {
	const normalized = raw.replace(/^\uFEFF/u, "").replace(/\r\n?/gu, "\n");
	if (normalized.length > maxBody * 2 || normalized.includes("\0"))
		return invalid();
	let value: unknown;
	if (format === "markdown") value = parseMarkdown(normalized);
	else {
		try {
			value = JSON.parse(normalized);
		} catch {
			return invalid();
		}
	}
	if (!object(value)) return invalid();
	const title = string(value.title) ?? incomplete();
	const url = sourceUrl(value.sourceUrl);
	const entityId = requiredTerm(value.entityId, terms, ["character"]);
	const range = value.gameVersions;
	if (!object(range) || value.guideKind !== "team") return incomplete();
	const version = (input: unknown) => {
		const text = string(input) ?? incomplete();
		if (gameVersionKey(text) === undefined) return incomplete();
		const [major, minor, patch] = text.split(".").map(Number);
		return patch ? `${major}.${minor}.${patch}` : `${major}.${minor}`;
	};
	const gameVersions = {
		from: version(range.from),
		through: version(range.through),
	};
	if (!versionApplies(gameVersions.from, gameVersions)) return incomplete();
	const data = value.applicability;
	if (!object(data)) return incomplete();
	let applicability: ImportDocument["applicability"];
	if (data.kind === "unrestricted" && Object.keys(data).length === 1)
		applicability = { kind: "unrestricted" };
	else if (data.kind === "known") {
		const conditions = list(data.conditions);
		const requiredCharacterIds = [
			...new Set(
				list(data.requiredCharacterIds).map((term) =>
					requiredTerm(term, terms, ["character"]),
				),
			),
		].sort();
		const requiredEquipmentIds = [
			...new Set(
				list(data.requiredEquipmentIds).map((term) =>
					requiredTerm(term, terms, ["weapon", "artifact"]),
				),
			),
		].sort();
		applicability = {
			kind: "known",
			conditions,
			requiredCharacterIds,
			requiredEquipmentIds,
		};
		if (!hasKnownPremise(applicability)) return incomplete();
	} else return incomplete();
	if (typeof value.body !== "string" || value.body.includes("\0"))
		return invalid();
	const lines = value.body
		.replace(/\r\n?/gu, "\n")
		.split("\n")
		.map((line) => line.trimEnd());
	const offset =
		format === "markdown" && typeof value.bodyOffset === "number"
			? value.bodyOffset
			: 0;
	let start = 0;
	let end = lines.length;
	while (start < end && !lines[start]) start++;
	while (end > start && !lines[end - 1]) end--;
	const content = lines.slice(start, end).join("\n");
	if (
		!content ||
		content.length > maxBody ||
		title.length > 300 ||
		content === title
	)
		return invalid();
	const contentHash = sha(
		JSON.stringify({ title, entityId, gameVersions, applicability, content }),
	);
	const id = sha(JSON.stringify([url, contentHash, processingVersion]));
	return {
		id,
		title,
		sourceUrl: url,
		entityId,
		gameVersions,
		applicability,
		content,
		contentHash,
		processingVersion,
		chunks: chunksFor(content, offset + start, id, entityId, terms),
	};
}

export class IngestGuide {
	constructor(
		private readonly repository: GuideWriteRepository,
		private readonly embeddings: EmbeddingPort,
		private readonly vectors: VectorWritePort,
		private readonly config: EmbeddingConfig,
	) {}

	private usable(
		vector: readonly number[] | undefined,
	): vector is readonly number[] {
		return (
			!!vector &&
			vector.length === this.config.dimensions &&
			vector.every(Number.isFinite) &&
			vector.some((n) => n !== 0)
		);
	}

	private async visible(
		guide: StoredGuide,
		signal: AbortSignal,
	): Promise<boolean> {
		for (let i = 0; i < guide.chunks.length; i += 32) {
			const ids = guide.chunks.slice(i, i + 32).map(({ id }) => id);
			const stored = await this.vectors.vectors(this.config.id, ids, signal);
			if (
				stored.size !== ids.length ||
				!ids.every((id) => this.usable(stored.get(id)))
			)
				return false;
		}
		return guide.chunks.length > 0;
	}

	async import(
		document: ImportDocument,
		publish: boolean,
		signal: AbortSignal,
	) {
		const guide = await this.repository.save(document, this.config, signal);
		try {
			if (!guide.indexed || !(await this.visible(guide, signal))) {
				const values = new Map<string, readonly number[]>();
				for (const chunk of guide.chunks) {
					signal.throwIfAborted();
					const vector = await this.embeddings.embed(
						chunk.body,
						this.config.id,
						signal,
					);
					if (!this.usable(vector)) throw new IngestError("INDEX_NOT_READY");
					values.set(chunk.id, vector);
				}
				await this.vectors.upsert(this.config.id, guide, values, signal);
				if (!(await this.visible(guide, signal)))
					throw new IngestError("INDEX_NOT_READY");
			}
		} catch {
			signal.throwIfAborted();
			throw new IngestError("INDEX_NOT_READY");
		}
		signal.throwIfAborted();
		if (!guide.indexed)
			await this.repository.finishIndex(guide.id, this.config.id, signal);
		if (publish && !guide.published)
			await this.repository.publish(guide.id, this.config.id, signal);
		return { id: guide.id, published: guide.published || publish };
	}

	async publish(id: string, signal: AbortSignal): Promise<void> {
		const guide = await this.repository.forPublication(
			id,
			this.config.id,
			signal,
		);
		if (!guide) throw new IngestError("GUIDE_NOT_FOUND");
		try {
			if (!guide.indexed || !(await this.visible(guide, signal)))
				throw new IngestError("INDEX_NOT_READY");
		} catch {
			signal.throwIfAborted();
			throw new IngestError("INDEX_NOT_READY");
		}
		signal.throwIfAborted();
		await this.repository.publish(id, this.config.id, signal);
	}
}

export function webSupplement(options: {
	source: GuideSource;
	permits: readonly SourcePermit[];
	parse: ParsePage;
	terms: readonly TerminologyEntry[];
	ingest: IngestGuide;
	archive: (page: IncompletePage, signal: AbortSignal) => Promise<void>;
}): WebSupplement {
	return async (scope, budget, signal) => {
		signal.throwIfAborted();
		const permits = options.permits.filter(
			(p) =>
				validPermit(p) &&
				p.access === "fetch_store_cite" &&
				options.source.canFetch(p),
		);
		if (!permits.length) return "source_restricted";
		// 仅由已确认范围构造出站参数，不转发模型自由文本、玩家条件或历史。
		const domains = [
			...new Set(permits.map((p) => `site:${new URL(p.origin).hostname}`)),
		];
		const query = [
			scope.entityId,
			scope.gameVersion,
			"配队",
			domains.join(" OR "),
		].join(" ");
		if (Array.from(query).length > 128)
			throw new TurnToolError("WEB_SOURCE_UNAVAILABLE");
		const request = async <T>(
			op: "search" | "fetch",
			run: (signal: AbortSignal) => Promise<T>,
		) => {
			signal.throwIfAborted();
			const timeout = budget.startSource(op);
			if (!timeout) throw new TurnToolError("BUDGET_EXHAUSTED");
			const active = AbortSignal.any([signal, budget.signal, timeout]);
			try {
				return await untilAborted(() => run(active), active);
			} catch {
				signal.throwIfAborted();
				budget.signal.throwIfAborted();
				throw new TurnToolError(
					timeout.aborted ? "TOOL_TIMEOUT" : "WEB_SOURCE_UNAVAILABLE",
				);
			}
		};
		const links = await request("search", (s) =>
			options.source.search(query, s),
		);
		if (!links.length) return "no_results";
		let outcome: SupplementStatus = "source_restricted";
		const seen = new Set<string>();
		for (const link of links) {
			signal.throwIfAborted();
			const permit = permits.find(
				(p) => permittedUrl(link, p) && options.source.canFetch(p),
			);
			if (!permit) continue;
			const url = permittedUrl(link, permit);
			if (!url || seen.has(url)) continue;
			if (seen.size >= 3) break;
			seen.add(url);
			const page = await request("fetch", (s) =>
				options.source.fetch(url, permit, s),
			);
			signal.throwIfAborted();
			const parsed = options.parse(page, url, permit.reference, options.terms);
			if (parsed.kind === "unreadable") {
				if (outcome === "source_restricted") outcome = "no_readable_body";
				continue;
			}
			if (parsed.kind === "incomplete") {
				await options.archive(parsed.page, signal).catch(() => {
					signal.throwIfAborted();
					throw new TurnToolError("RETRIEVAL_UNAVAILABLE");
				});
				outcome = "metadata_incomplete";
				continue;
			}
			const doc = parsed.document;
			if (
				doc.entityId !== scope.entityId ||
				!versionApplies(scope.gameVersion, doc.gameVersions)
			) {
				outcome = "scope_mismatch";
				continue;
			}
			if (
				doc.sourceUrl !== url ||
				doc.webEvidence?.permission !== permit.reference
			)
				throw new TurnToolError("WEB_SOURCE_UNAVAILABLE");
			try {
				await options.ingest.import(doc, true, signal);
			} catch (error) {
				signal.throwIfAborted();
				throw new TurnToolError(
					error instanceof IngestError && error.code === "INDEX_NOT_READY"
						? "INDEX_NOT_READY"
						: "RETRIEVAL_UNAVAILABLE",
				);
			}
			return "indexed";
		}
		return outcome;
	};
}
