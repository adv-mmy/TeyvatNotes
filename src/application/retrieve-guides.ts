import { rankGuides } from "../domain/guide-ranking.js";
import {
	hasKnownPremise,
	matchesVersionLayer,
	type QueryScope,
	versionLayers,
} from "../domain/query-scope.js";
import {
	type GuideChunkMetadata,
	type GuideData,
	type GuideFilter,
	type GuidePage,
	type GuideSnapshot,
	GuideSnapshotChanged,
	type GuideWindow,
} from "./ports/guide-data.js";

export interface RetrievalLimits {
	readonly pageSize: number;
	readonly maxPages: number;
	readonly maxScanned: number;
	readonly maxCandidates: number;
	readonly minVectorScore: number;
}

export const defaultRetrievalLimits: RetrievalLimits = {
	pageSize: 20,
	maxPages: 4,
	maxScanned: 160,
	maxCandidates: 8,
	minVectorScore: 0,
};

export interface AuthorizedCandidate {
	readonly chunk: GuideChunkMetadata;
	readonly snapshot: GuideSnapshot;
	readonly versionLayer: string | null;
}
export type RetrievalError = "RETRIEVAL_INCOMPLETE" | "RETRIEVAL_UNAVAILABLE";
export type GuideSearchOutcome =
	| {
			readonly kind: "hit";
			readonly candidates: readonly AuthorizedCandidate[];
	  }
	| { readonly kind: "miss" }
	| { readonly kind: "error"; readonly code: RetrievalError };
export type GuideReadOutcome =
	| { readonly kind: "complete"; readonly window: GuideWindow }
	| { readonly kind: "incomplete" }
	| {
			readonly kind: "error";
			readonly code: RetrievalError | "CANDIDATE_NOT_AUTHORIZED";
	  };

function filterOf(scope: QueryScope, versionLayer: string | null): GuideFilter {
	return {
		entityId: scope.entityId,
		gameVersion: versionLayer,
		guideKind: scope.guideKind,
	};
}

function validUrl(value: string): boolean {
	try {
		const url = new URL(value);
		return url.protocol === "https:" && !url.username && !url.password;
	} catch {
		return false;
	}
}

function matches(
	scope: QueryScope,
	item: GuideChunkMetadata,
	versionLayer: string | null,
): boolean {
	if (
		!item.published ||
		!item.indexReady ||
		item.entityId !== scope.entityId ||
		item.guideKind !== scope.guideKind ||
		!matchesVersionLayer(versionLayer, item.gameVersions) ||
		(item.applicability.kind === "unknown" && !item.fromWeb) ||
		!item.id.trim() ||
		!item.documentVersionId.trim() ||
		!item.title.trim() ||
		!item.location.trim() ||
		!validUrl(item.sourceUrl)
	)
		return false;
	if (item.applicability.kind === "known") {
		if (!hasKnownPremise(item.applicability)) return false;
	}
	return true;
}

function errorCode(error: unknown): RetrievalError {
	return error instanceof GuideSnapshotChanged
		? "RETRIEVAL_INCOMPLETE"
		: "RETRIEVAL_UNAVAILABLE";
}

interface Branch {
	readonly ranked: readonly string[];
	readonly exhausted: boolean;
}
function assertVector(vector: readonly number[], dimension?: number): void {
	if (
		!vector.length ||
		(dimension !== undefined && dimension !== vector.length) ||
		!vector.every(Number.isFinite) ||
		!Number.isFinite(Math.hypot(...vector)) ||
		Math.hypot(...vector) === 0
	)
		throw new Error("INVALID_VECTOR");
}

function cosine(vector: readonly number[], other: readonly number[]): number {
	assertVector(other, vector.length);
	const a = Math.hypot(...vector);
	const b = Math.hypot(...other);
	return vector.reduce(
		(sum, value, index) => sum + (value / a) * ((other[index] ?? 0) / b),
		0,
	);
}

function bySimilarity(scores: ReadonlyMap<string, number>): string[] {
	return [...scores]
		.sort(([a, x], [b, y]) => y - x || (a < b ? -1 : a > b ? 1 : 0))
		.map(([id]) => id);
}

export class GuideRetrieval {
	constructor(
		private readonly data: GuideData,
		private readonly limits: RetrievalLimits = defaultRetrievalLimits,
	) {
		for (const n of [
			limits.pageSize,
			limits.maxPages,
			limits.maxScanned,
			limits.maxCandidates,
		])
			if (!Number.isSafeInteger(n) || n < 1)
				throw new RangeError("无效的检索上限");
		if (
			!Number.isFinite(limits.minVectorScore) ||
			limits.minVectorScore < -1 ||
			limits.minVectorScore > 1
		)
			throw new RangeError("无效的向量相似度门槛");
	}

	async searchGuides(
		scope: QueryScope,
		query: string,
		signal: AbortSignal,
		lockedLayer?: string | null,
	): Promise<GuideSearchOutcome> {
		const layers = versionLayers(scope.versionScope);
		if (lockedLayer !== undefined && !layers.includes(lockedLayer))
			return { kind: "error", code: "RETRIEVAL_UNAVAILABLE" };
		for (const layer of lockedLayer === undefined ? layers : [lockedLayer]) {
			const result = await this.searchLayer(scope, query, signal, layer);
			if (result.kind !== "miss") return result;
		}
		return { kind: "miss" };
	}

	/** 只探测候选是否存在，不读取正文、不授予候选 ID。 */
	async hasUnknownReference(
		scope: QueryScope,
		signal: AbortSignal,
	): Promise<boolean> {
		const filter = filterOf(scope, null);
		const { repository, vectorIndex } = this.data;
		try {
			const snapshot = await repository.snapshot(filter, signal);
			const eligible = await this.walk(
				(cursor, limit) =>
					repository.eligibleIds(filter, snapshot, cursor, limit, signal),
				async (ids) => {
					if (!ids.length) return [];
					const values = await repository.metadata(snapshot, ids, signal);
					if (
						values.length !== ids.length ||
						new Set(values.map((item) => item.id)).size !== ids.length ||
						values.some((item) => !ids.includes(item.id))
					)
						throw new Error("INVALID_METADATA");
					return values
						.filter((item) => matches(scope, item, null))
						.map(({ id }) => id);
				},
				signal,
				true,
				1,
			);
			const [id] = eligible.ranked;
			if (id && !(await vectorIndex.hasAll(snapshot.embeddingId, [id], signal)))
				throw new Error("MISSING_INDEX_VECTOR");
			await repository.assertSnapshot(filter, snapshot, signal);
			if (!id && !eligible.exhausted) throw new GuideSnapshotChanged();
			return id !== undefined;
		} catch (error) {
			signal.throwIfAborted();
			throw new Error(errorCode(error));
		}
	}

	private async searchLayer(
		scope: QueryScope,
		query: string,
		signal: AbortSignal,
		versionLayer: string | null,
	): Promise<GuideSearchOutcome> {
		const filter = filterOf(scope, versionLayer);
		const branches = new AbortController();
		const branchSignal = AbortSignal.any([signal, branches.signal]);
		try {
			signal.throwIfAborted();
			const snapshot = await this.data.repository.snapshot(filter, signal);
			const chunks = new Map<string, GuideChunkMetadata>();
			const eligible = async (
				ids: readonly string[],
				strict = false,
			): Promise<string[]> => {
				branchSignal.throwIfAborted();
				if (!ids.length) return [];
				const unique = new Set(ids);
				const values = await this.data.repository.metadata(
					snapshot,
					[...unique],
					branchSignal,
				);
				branchSignal.throwIfAborted();
				const received = new Set<string>();
				const qualified = new Set<string>();
				for (const item of values) {
					if (!unique.has(item.id) || received.has(item.id))
						throw new Error("INVALID_METADATA");
					received.add(item.id);
					if (matches(scope, item, versionLayer)) {
						chunks.set(item.id, item);
						qualified.add(item.id);
					}
				}
				if (strict && received.size !== unique.size)
					throw new Error("MISSING_METADATA");
				return [...unique].filter((id) => qualified.has(id));
			};
			const [keywords, semantic] = await Promise.all([
				this.keyword(filter, snapshot, query, eligible, branchSignal),
				this.semantic(filter, snapshot, query, eligible, branchSignal),
			]);
			signal.throwIfAborted();
			await this.data.repository.assertSnapshot(filter, snapshot, signal);
			signal.throwIfAborted();
			if (!semantic.exhausted)
				return { kind: "error", code: "RETRIEVAL_INCOMPLETE" };
			const ranked = rankGuides([keywords.ranked, semantic.ranked]).slice(
				0,
				this.limits.maxCandidates,
			);
			const candidates = ranked.flatMap(({ id }) => {
				const chunk = chunks.get(id);
				return chunk ? [{ chunk, snapshot, versionLayer }] : [];
			});
			if (candidates.length) return { kind: "hit", candidates };
			return keywords.exhausted && semantic.exhausted
				? { kind: "miss" }
				: { kind: "error", code: "RETRIEVAL_INCOMPLETE" };
		} catch (error) {
			signal.throwIfAborted();
			return { kind: "error", code: errorCode(error) };
		} finally {
			branches.abort();
		}
	}

	async readGuide(
		scope: QueryScope,
		candidate: AuthorizedCandidate,
		signal: AbortSignal,
	): Promise<GuideReadOutcome> {
		try {
			signal.throwIfAborted();
			const { versionLayer } = candidate;
			const filter = filterOf(scope, versionLayer);
			if (
				!versionLayers(scope.versionScope).includes(versionLayer) ||
				!matches(scope, candidate.chunk, versionLayer)
			)
				return { kind: "error", code: "CANDIDATE_NOT_AUTHORIZED" };
			const row = await this.data.repository.read(
				candidate.snapshot,
				candidate.chunk.id,
				signal,
			);
			signal.throwIfAborted();
			if (!row) return { kind: "error", code: "RETRIEVAL_UNAVAILABLE" };
			if (
				row.chunk.id !== candidate.chunk.id ||
				row.chunk.documentVersionId !== candidate.chunk.documentVersionId ||
				!matches(scope, row.chunk, versionLayer)
			)
				return { kind: "error", code: "CANDIDATE_NOT_AUTHORIZED" };
			await this.data.repository.assertSnapshot(
				filter,
				candidate.snapshot,
				signal,
			);
			signal.throwIfAborted();
			return row.complete && row.body.trim()
				? { kind: "complete", window: row }
				: { kind: "incomplete" };
		} catch (error) {
			signal.throwIfAborted();
			return { kind: "error", code: errorCode(error) };
		}
	}

	private async keyword(
		filter: GuideFilter,
		snapshot: GuideSnapshot,
		query: string,
		eligible: (ids: readonly string[], strict?: boolean) => Promise<string[]>,
		signal: AbortSignal,
	): Promise<Branch> {
		return this.walk(
			(cursor, limit) =>
				this.data.repository.keywords(
					filter,
					snapshot,
					query,
					cursor,
					limit,
					signal,
				),
			(ids) => eligible(ids, true),
			signal,
		);
	}

	private async semantic(
		filter: GuideFilter,
		snapshot: GuideSnapshot,
		query: string,
		eligible: (ids: readonly string[], strict?: boolean) => Promise<string[]>,
		signal: AbortSignal,
	): Promise<Branch> {
		const { vectorIndex, embeddings, repository } = this.data;
		const { embeddingId } = snapshot;
		const { pageSize, maxScanned, maxPages, minVectorScore, maxCandidates } =
			this.limits;
		signal.throwIfAborted();
		const vector = await embeddings.embed(query, embeddingId, signal);
		assertVector(vector);
		let size = Math.min(pageSize, maxScanned);
		const seen = new Set<string>();
		const present = new Set<string>();
		const approximate = new Map<string, number>();
		for (let page = 0; page < maxPages; page++) {
			signal.throwIfAborted();
			const nearest = await vectorIndex.nearest(
				embeddingId,
				vector,
				size,
				signal,
				filter,
			);
			if (
				nearest.length > size ||
				nearest.some((item) => !item.id || !Number.isFinite(item.score))
			)
				throw new Error("INVALID_NEAREST_RESULT");
			for (const { id } of nearest) present.add(id);
			const scored = nearest.filter(({ score }) => score >= minVectorScore);
			const hits = new Set(
				await eligible(
					scored.filter(({ id }) => !seen.has(id)).map(({ id }) => id),
				),
			);
			for (const { id, score } of scored) {
				seen.add(id);
				if (hits.has(id) || approximate.has(id))
					approximate.set(
						id,
						Math.max(score, approximate.get(id) ?? -Infinity),
					);
			}
			if (approximate.size >= maxCandidates) break;
			if (size === maxScanned) break;
			size = Math.min(size * 2, maxScanned);
		}
		// 有近邻不证明索引完整。只按页核对 ID，不读取额外向量、扩充排名或
		// 因一个健康范围大于排名预算就误报故障。核对始终受同一工具/轮次时限约束。
		if (approximate.size) {
			let cursor: string | null = null;
			const cursors = new Set<string>();
			do {
				signal.throwIfAborted();
				const page: GuidePage = await repository.eligibleIds(
					filter,
					snapshot,
					cursor,
					256,
					signal,
				);
				const unchecked = page.ids.filter((id) => !present.has(id));
				if (
					unchecked.length &&
					!(await vectorIndex.hasAll(embeddingId, unchecked, signal))
				)
					throw new Error("MISSING_INDEX_VECTOR");
				cursor = page.next;
				if (cursor !== null) {
					if (!page.ids.length || cursors.has(cursor))
						return { ranked: [], exhausted: false };
					cursors.add(cursor);
				}
			} while (cursor !== null);
			return { ranked: bySimilarity(approximate), exhausted: true };
		}
		// ANN 未命中不证明查完：在排名预算内按权威清单补判相似度。
		const scores = new Map<string, number>();
		const coverage = await this.walk(
			(cursor, limit) =>
				repository.eligibleIds(filter, snapshot, cursor, limit, signal),
			async (ids) => {
				if (!ids.length) return [];
				const qualified = await eligible(ids, true);
				signal.throwIfAborted();
				const vectors = await vectorIndex.vectors(embeddingId, ids, signal);
				for (const id of ids) {
					const stored = vectors.get(id);
					if (!stored) throw new Error("MISSING_INDEX_VECTOR");
					const score = cosine(vector, stored);
					if (qualified.includes(id) && score >= minVectorScore)
						scores.set(id, score);
				}
				return [];
			},
			signal,
			false,
		);
		return {
			exhausted: coverage.exhausted,
			ranked: bySimilarity(scores),
		};
	}

	private async walk(
		load: (cursor: string | null, limit: number) => Promise<GuidePage>,
		select: (ids: readonly string[]) => Promise<string[]>,
		signal: AbortSignal,
		stopWhenEnough = true,
		targetCount = this.limits.maxCandidates,
	): Promise<Branch> {
		const { pageSize, maxScanned, maxPages } = this.limits;
		const ids = new Set<string>();
		const cursors = new Set<string>();
		const ranked: string[] = [];
		let cursor: string | null = null;
		let scanned = 0;
		for (let page = 0; page < maxPages && scanned < maxScanned; page++) {
			signal.throwIfAborted();
			const limit = Math.min(pageSize, maxScanned - scanned);
			const result = await load(cursor, limit);
			if (result.ids.length > limit || result.ids.some((id) => !id))
				throw new Error("INVALID_PAGE");
			scanned += result.ids.length;
			const fresh = [...new Set(result.ids)].filter((id) => !ids.has(id));
			for (const id of fresh) ids.add(id);
			ranked.push(...(await select(fresh)));
			if (result.next === null) return { ranked, exhausted: true };
			if (
				(stopWhenEnough && ranked.length >= targetCount) ||
				cursors.has(result.next)
			)
				return { ranked, exhausted: false };
			cursors.add(result.next);
			cursor = result.next;
		}
		return { ranked, exhausted: false };
	}
}
