import type {
	EmbeddingConfig,
	EmbeddingPort,
} from "../../application/ports/guide-data.js";

export class OllamaEmbedder implements EmbeddingPort {
	constructor(
		private readonly origin: URL,
		private readonly config: EmbeddingConfig,
	) {}

	async embed(
		text: string,
		embeddingId: string,
		signal: AbortSignal,
	): Promise<readonly number[]> {
		signal.throwIfAborted();
		if (embeddingId !== this.config.id)
			throw new Error("EMBEDDING_CONFIG_MISMATCH");
		try {
			const response = await fetch(new URL("/api/embed", this.origin), {
				method: "POST",
				redirect: "error",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({
					model: this.config.model,
					input: text,
					dimensions: this.config.dimensions,
					truncate: false,
				}),
				signal,
			});
			if (!response.ok) {
				await response.body?.cancel();
				throw new Error("EMBEDDING_UNAVAILABLE");
			}
			const result: unknown = await response.json();
			if (
				typeof result !== "object" ||
				!result ||
				!("embeddings" in result) ||
				!Array.isArray(result.embeddings) ||
				result.embeddings.length !== 1
			)
				throw new Error("EMBEDDING_UNAVAILABLE");
			const vector: unknown = result.embeddings[0];
			if (
				!Array.isArray(vector) ||
				vector.length !== this.config.dimensions ||
				!vector.every((n) => typeof n === "number" && Number.isFinite(n)) ||
				!vector.some((n) => n !== 0)
			)
				throw new Error("EMBEDDING_UNAVAILABLE");
			signal.throwIfAborted();
			return vector as number[];
		} catch {
			signal.throwIfAborted();
			throw new Error("EMBEDDING_UNAVAILABLE");
		}
	}
}
