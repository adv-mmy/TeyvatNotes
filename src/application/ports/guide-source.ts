import type { TerminologyEntry } from "../../domain/query-scope.js";
import type { ImportDocument } from "./guide-data.js";

export interface SourcePermit {
	readonly origin: string;
	readonly path: string;
	readonly reference: string;
	readonly access: "link" | "fetch_store_cite";
}
export interface PageBody {
	readonly title: string;
	readonly content: string;
}
export interface FieldProof {
	readonly location: string;
	readonly excerpt: string;
}
export type FieldProofs = Readonly<Record<string, FieldProof>>;
export interface IncompletePage extends PageBody {
	readonly url: string;
	readonly permission: string;
	readonly proofs: FieldProofs;
	readonly reasons: readonly string[];
}
export type ParsedPage =
	| { kind: "ready"; document: ImportDocument }
	| { kind: "incomplete"; page: IncompletePage }
	| { kind: "unreadable" };
export type ParsePage = (
	body: PageBody,
	url: string,
	permission: string,
	terms: readonly TerminologyEntry[],
) => ParsedPage;

/** 只向供应商传递公开检索词，不把会话或玩家清单放到这个接口上。 */
export type SupplementStatus =
	| "indexed"
	| "no_results"
	| "source_restricted"
	| "no_readable_body"
	| "metadata_incomplete"
	| "scope_mismatch";
export type WebSupplement = (
	scope: import("./guide-data.js").GuideFilter,
	budget: import("../turn-budget.js").TurnBudget,
	signal: AbortSignal,
) => Promise<SupplementStatus>;

export interface GuideSource {
	search(query: string, signal: AbortSignal): Promise<readonly string[]>;
	/** 供应商能否在读取正文前保证每一跳都受许可和网络边界约束。 */
	canFetch(permit: SourcePermit): boolean;
	fetch(
		url: string,
		permit: SourcePermit,
		signal: AbortSignal,
	): Promise<PageBody>;
}

export function validPermit(value: unknown): value is SourcePermit {
	if (!value || typeof value !== "object") return false;
	const p = value as Partial<SourcePermit>;
	if (
		typeof p.origin !== "string" ||
		typeof p.path !== "string" ||
		typeof p.reference !== "string" ||
		!p.reference.trim() ||
		!["link", "fetch_store_cite"].includes(p.access ?? "")
	)
		return false;
	try {
		const u = new URL(p.origin);
		return (
			u.origin === p.origin &&
			u.protocol === "https:" &&
			!u.port &&
			/^(?:[a-z0-9-]+\.)+[a-z]{2,}$/u.test(u.hostname) &&
			!/(^|\.)(?:localhost|local|internal|lan|home|test)$/u.test(u.hostname) &&
			/^\/(?:[a-zA-Z0-9_-]+\/)*$/u.test(p.path)
		);
	} catch {
		return false;
	}
}

export function permittedUrl(
	raw: string,
	permit: SourcePermit,
): string | undefined {
	if (
		!validPermit(permit) ||
		permit.access !== "fetch_store_cite" ||
		raw.length > 2048 ||
		/[%\\\s\p{Cc}]/u.test(raw) ||
		/\/\.{1,2}(?:\/|$)/u.test(raw)
	)
		return;
	try {
		const u = new URL(raw);
		if (
			u.origin !== permit.origin ||
			u.username ||
			u.password ||
			u.search ||
			!u.pathname.startsWith(permit.path) ||
			u.pathname.includes("//")
		)
			return;
		u.hash = "";
		return u.href;
	} catch {
		return;
	}
}
