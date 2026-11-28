import { untilAborted } from "../../application/ports/agent.js";
import {
	type GuideSource,
	permittedUrl,
	type SourceCandidate,
	type SourcePermit,
	type SourceRequestBudget,
	type SourceRevision,
	validPermit,
} from "../../application/ports/guide-source.js";

const origin = "https://wiki.biligame.com";
const articleRoot = "/ys/";
export const bwikiApi = `${origin}${articleRoot}api.php`;
const teamSection =
	/^(?:配队|配队建议|配队推荐|配队思路|推荐阵容|阵容搭配|队伍搭配|队伍推荐)$/u;
const record = (value: unknown): value is Record<string, unknown> =>
	typeof value === "object" && value !== null && !Array.isArray(value);
const sameTitle = (a: string, b: string) =>
	a.replaceAll(" ", "_") === b.replaceAll(" ", "_");
const unavailable = () => new Error("WEB_SOURCE_UNAVAILABLE");

export function wikiHeading(line: string) {
	const heading = /^(={1,6})\s*([^=]+?)\s*\1\s*$/u.exec(line);
	return heading?.[1] && heading[2]
		? { depth: heading[1].length, title: heading[2].trim() }
		: undefined;
}

function parseSection(value: unknown): SourceRevision["section"] {
	if (
		!record(value) ||
		typeof value.index !== "string" ||
		typeof value.line !== "string" ||
		typeof value.anchor !== "string"
	)
		throw unavailable();
	return { index: value.index, title: value.line.trim(), anchor: value.anchor };
}

function parsePage(response: unknown, title: string) {
	if (
		!record(response) ||
		!record(response.parse) ||
		typeof response.parse.title !== "string" ||
		!sameTitle(response.parse.title, title) ||
		typeof response.parse.revid !== "number" ||
		!Number.isSafeInteger(response.parse.revid) ||
		response.parse.revid <= 0 ||
		!record(response.parse.wikitext) ||
		typeof response.parse.wikitext["*"] !== "string"
	)
		throw unavailable();
	return {
		parse: response.parse,
		content: response.parse.wikitext["*"],
		id: response.parse.revid,
	};
}

export class BwikiSource implements GuideSource {
	constructor(private readonly http: typeof fetch = fetch) {}

	canFetch(permit: SourcePermit, candidate?: SourceCandidate): boolean {
		if (
			!validPermit(permit) ||
			permit.origin !== origin ||
			permit.api !== bwikiApi ||
			!permit.path.startsWith(articleRoot)
		)
			return false;
		if (!candidate) return true;
		const url = permittedUrl(candidate.url, permit);
		if (!url) return false;
		const path = decodeURIComponent(new URL(url).pathname);
		return (
			path.startsWith(articleRoot) &&
			path !== `${articleRoot}api.php` &&
			candidate.title.length <= 300 &&
			sameTitle(path.slice(articleRoot.length), candidate.title)
		);
	}

	private async request(
		operation: "discover" | "read",
		params: Record<string, string>,
		requests: SourceRequestBudget,
	): Promise<unknown> {
		const url = new URL(bwikiApi);
		url.search = new URLSearchParams({
			...params,
			format: "json",
			formatversion: "1",
		}).toString();
		const signal = requests.begin(operation);
		return untilAborted(async () => {
			const response = await this.http(url, {
				method: "GET",
				redirect: "error",
				credentials: "omit",
				headers: { Accept: "application/json" },
				signal,
			});
			if (
				!response.ok ||
				response.redirected ||
				!response.body ||
				!/^application\/json(?:\s*;|$)/iu.test(
					response.headers.get("content-type") ?? "",
				)
			) {
				void response.body?.cancel().catch(() => {});
				throw unavailable();
			}
			const reader = response.body.getReader();
			const chunks: Uint8Array[] = [];
			let size = 0;
			try {
				for (;;) {
					signal.throwIfAborted();
					const { value, done } = await reader.read();
					if (done) break;
					size += value.byteLength;
					if (size > 1_000_000) throw unavailable();
					chunks.push(value);
				}
				const value: unknown = JSON.parse(
					new TextDecoder("utf-8", { fatal: true }).decode(
						Buffer.concat(chunks),
					),
				);
				if (record(value) && Object.hasOwn(value, "error")) throw unavailable();
				return value;
			} catch (error) {
				void reader.cancel().catch(() => {});
				throw error;
			} finally {
				reader.releaseLock();
			}
		}, signal);
	}

	async search(query: string, requests: SourceRequestBudget) {
		if (!/^[\p{L}\p{M}\p{N} ·—()._-]{1,128}$/u.test(query) || !query.trim())
			throw unavailable();
		const found = await this.request(
			"discover",
			{
				action: "opensearch",
				search: query,
				namespace: "0",
				limit: "10",
				redirects: "return",
			},
			requests,
		);
		if (
			!Array.isArray(found) ||
			found.length !== 4 ||
			found[0] !== query ||
			!Array.isArray(found[1]) ||
			!Array.isArray(found[2]) ||
			!Array.isArray(found[3]) ||
			found[1].length !== found[3].length ||
			found[1].length > 10
		)
			throw unavailable();
		const titles: readonly unknown[] = found[1];
		const urls: readonly unknown[] = found[3];
		const candidates = Array.from(titles, (title, index): SourceCandidate => {
			const url = urls[index];
			if (
				typeof title !== "string" ||
				!title.trim() ||
				typeof url !== "string" ||
				!url
			)
				throw unavailable();
			return { title, url };
		});
		// 精确攻略优先，其次是角色名下的攻略子页。同级保留上游顺序。
		const prefix = `${query.replaceAll(" ", "_")}/`;
		const priority = ({ title }: SourceCandidate) => {
			const normalized = title.replaceAll(" ", "_");
			if (normalized === `${prefix}攻略`) return 2;
			return normalized.startsWith(prefix) && normalized.endsWith("/攻略")
				? 1
				: 0;
		};
		return candidates.sort((a, b) => priority(b) - priority(a));
	}

	async fetch(
		candidate: SourceCandidate,
		permit: SourcePermit,
		requests: SourceRequestBudget,
	) {
		if (!this.canFetch(permit, candidate)) throw new Error("SOURCE_RESTRICTED");
		const page = parsePage(
			await this.request(
				"read",
				{
					action: "parse",
					page: candidate.title,
					prop: "wikitext|sections|revid",
				},
				requests,
			),
			candidate.title,
		);
		if (!Array.isArray(page.parse.sections)) throw unavailable();
		const sections = (page.parse.sections as unknown[])
			.map(parseSection)
			.filter((section) => teamSection.test(section.title));
		const section = sections[0];
		if (!section || sections.length !== 1) return undefined;
		if (
			!/^[1-9]\d{0,7}$/u.test(section.index) ||
			!section.anchor ||
			section.anchor.length > 300 ||
			/[\p{Cc}\p{Cf}]/u.test(section.anchor)
		)
			throw unavailable();
		const body = parsePage(
			await this.request(
				"read",
				{
					action: "parse",
					oldid: String(page.id),
					section: section.index,
					prop: "wikitext|revid",
				},
				requests,
			),
			candidate.title,
		);
		if (body.id !== page.id) throw unavailable();
		if (!body.content.trim()) return undefined;
		const firstLine =
			body.content
				.replace(/^\uFEFF/u, "")
				.trimStart()
				.split(/\r?\n/u, 1)[0] ?? "";
		if (wikiHeading(firstLine)?.title !== section.title) throw unavailable();
		return {
			title: candidate.title,
			content: body.content,
			revision: { id: page.id, section },
		};
	}
}
