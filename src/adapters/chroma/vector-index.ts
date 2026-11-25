import { ChromaClient, ChromaNotFoundError, IncludeEnum } from "chromadb";
import type {
	EmbeddingConfig,
	GuideFilter,
	StoredGuide,
	VectorWritePort,
} from "../../application/ports/guide-data.js";
import { gameVersionKey } from "../../domain/query-scope.js";

export class ChromaVectorIndex implements VectorWritePort {
	constructor(
		private readonly origin: URL,
		private readonly prefix: string,
		private readonly config: EmbeddingConfig,
	) {}

	private options(embeddingId: string, signal: AbortSignal) {
		signal.throwIfAborted();
		if (embeddingId !== this.config.id)
			throw new Error("EMBEDDING_CONFIG_MISMATCH");
		return {
			client: new ChromaClient({
				host: this.origin.hostname,
				port: Number(
					this.origin.port || (this.origin.protocol === "https:" ? 443 : 80),
				),
				ssl: this.origin.protocol === "https:",
				fetchOptions: { signal, redirect: "error" },
			}),
			name: `${this.prefix}_${embeddingId}`,
			embeddingFunction: null,
		};
	}

	private async existing(embeddingId: string, signal: AbortSignal) {
		const { client, name } = this.options(embeddingId, signal);
		try {
			return await client.getCollection({ name });
		} catch (error) {
			if (error instanceof ChromaNotFoundError) return null;
			throw error;
		}
	}

	async nearest(
		embeddingId: string,
		vector: readonly number[],
		limit: number,
		signal: AbortSignal,
		filter?: GuideFilter,
	) {
		const collection = await this.existing(embeddingId, signal);
		if (!collection) return [];
		const version = filter ? gameVersionKey(filter.gameVersion) : undefined;
		if (filter && version === undefined)
			throw new Error("INVALID_GAME_VERSION");
		const result = await collection.query({
			queryEmbeddings: [[...vector]],
			nResults: limit,
			include: [IncludeEnum.distances],
			...(filter && version !== undefined
				? {
						where: {
							$and: [
								{ entity: { $eq: filter.entityId } },
								{ kind: { $eq: filter.guideKind } },
								{ game_from: { $lte: version } },
								{ game_through: { $gte: version } },
							],
						},
					}
				: {}),
		});
		const [rows] = result.rows();
		if (!rows) throw new Error("INVALID_VECTOR_RESULT");
		return rows.map(({ id, distance }) => {
			if (typeof distance !== "number" || !Number.isFinite(distance))
				throw new Error("INVALID_VECTOR_RESULT");
			return { id, score: 1 - distance };
		});
	}

	async vectors(
		embeddingId: string,
		ids: readonly string[],
		signal: AbortSignal,
	) {
		const collection = await this.existing(embeddingId, signal);
		if (!collection || !ids.length) return new Map<string, number[]>();
		const result = await collection.get({
			ids: [...ids],
			include: [IncludeEnum.embeddings],
		});
		const vectors = new Map<string, number[]>();
		const requested = new Set(ids);
		for (const { id, embedding } of result.rows()) {
			if (!embedding || !requested.has(id) || vectors.has(id))
				throw new Error("INVALID_VECTOR_RESULT");
			vectors.set(id, embedding);
		}
		return vectors;
	}

	async hasAll(
		embeddingId: string,
		ids: readonly string[],
		signal: AbortSignal,
	) {
		if (!ids.length) return true;
		const collection = await this.existing(embeddingId, signal);
		if (!collection) return false;
		const found = new Set(
			(await collection.get({ ids: [...ids], include: [] })).ids,
		);
		return ids.every((id) => found.has(id));
	}

	async upsert(
		embeddingId: string,
		guide: StoredGuide,
		vectors: ReadonlyMap<string, readonly number[]>,
		signal: AbortSignal,
	): Promise<void> {
		const { client, ...options } = this.options(embeddingId, signal);
		const from = gameVersionKey(guide.gameVersions.from);
		const through = gameVersionKey(guide.gameVersions.through);
		if (from === undefined || through === undefined)
			throw new Error("INVALID_GAME_VERSION");
		const collection = await client.getOrCreateCollection({
			...options,
			configuration: { hnsw: { space: "cosine" } },
		});
		for (let i = 0; i < guide.chunks.length; i += 32) {
			signal.throwIfAborted();
			const ids = guide.chunks.slice(i, i + 32).map(({ id }) => id);
			await collection.upsert({
				ids,
				embeddings: ids.map((id) => {
					const vector = vectors.get(id);
					if (vector?.length !== this.config.dimensions)
						throw new Error("INVALID_VECTOR");
					return [...vector];
				}),
				metadatas: ids.map(() => ({
					entity: guide.entityId,
					kind: "team",
					game_from: from,
					game_through: through,
				})),
			});
		}
	}
}
