import type {
	FrozenQueryScope,
	TerminologyEntry,
} from "../../domain/query-scope.js";
import type { ImportDocument } from "./guide-data.js";

export interface SourcePermit {
	readonly origin: string;
	readonly api: string;
	readonly path: string;
	readonly reference: string;
	readonly access: "fetch_store_cite";
}
export interface SourceCandidate {
	readonly title: string;
	readonly url: string;
}
export interface SourceRevision {
	readonly id: number;
	readonly section: {
		readonly index: string;
		readonly title: string;
		readonly anchor: string;
	};
}
export interface PageBody {
	readonly title: string;
	readonly content: string;
	readonly revision: SourceRevision;
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

export type SupplementStatus =
	| "indexed"
	| Exclude<
			import("../../domain/answer-validation.js").SupplementGap,
			"recheck_miss"
	  >;
export type WebSupplement = (
	scope: FrozenQueryScope,
	budget: import("../turn-budget.js").TurnBudget,
	signal: AbortSignal,
) => Promise<SupplementStatus>;

export interface GuideSource {
	search(
		query: string,
		requests: SourceRequestBudget,
	): Promise<readonly SourceCandidate[]>;
	/** 不提供候选时，只检查是否存在获准且可控的读取路径。 */
	canFetch(permit: SourcePermit, candidate?: SourceCandidate): boolean;
	fetch(
		candidate: SourceCandidate,
		permit: SourcePermit,
		requests: SourceRequestBudget,
	): Promise<PageBody | undefined>;
}

export interface SourceRequestBudget {
	begin(operation: "discover" | "read"): AbortSignal;
}

function normalizedPath(path: string): string | undefined {
	if (!path.startsWith("/") || path.length > 2048 || path.includes("//"))
		return;
	try {
		const parts = path.slice(1).split("/");
		const decoded = parts.map(decodeURIComponent);
		if (
			decoded.some(
				(part, i) =>
					(part === "" && i !== decoded.length - 1) ||
					part === "." ||
					part === ".." ||
					!/^[\p{L}\p{M}\p{N} _.()·—-]*$/u.test(part),
			)
		)
			return;
		return `/${decoded.map((part) => part.replaceAll(" ", "_")).join("/")}`;
	} catch {
		return;
	}
}

export function validPermit(value: unknown): value is SourcePermit {
	if (!value || typeof value !== "object") return false;
	const p = value as Partial<SourcePermit>;
	if (
		typeof p.origin !== "string" ||
		typeof p.api !== "string" ||
		typeof p.path !== "string" ||
		typeof p.reference !== "string" ||
		!p.reference.trim() ||
		p.access !== "fetch_store_cite"
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
			p.api.startsWith(`${p.origin}/`) &&
			/^\/[a-zA-Z0-9_/-]+\.php$/u.test(p.api.slice(p.origin.length)) &&
			normalizedPath(p.path) !== undefined
		);
	} catch {
		return false;
	}
}

export function permittedUrl(
	raw: string,
	permit: SourcePermit,
): string | undefined {
	if (!validPermit(permit) || !raw.startsWith(`${permit.origin}/`)) return;
	// 先检查原始路径，避免 URL 构造器先折叠点路径或反斜杠。
	const path = normalizedPath(raw.slice(permit.origin.length));
	const allowed = normalizedPath(permit.path);
	if (!path || !allowed || path.endsWith("/")) return;
	if (allowed.endsWith("/") ? !path.startsWith(allowed) : path !== allowed)
		return;
	return new URL(path, permit.origin).href;
}
