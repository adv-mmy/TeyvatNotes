/** 仅传入可安全前置筛选的范围，不把玩家库存交给存储或 Embedding。 */
export interface GuideFilter {
	readonly entityId: string;
	readonly gameVersion: string;
	readonly guideKind: "team";
}

/** 查询过程中范围或索引就绪记录变化时，适配器应显式报告。 */
export class GuideSnapshotChanged extends Error {}

export interface GuideSnapshot {
	readonly revision: string;
	readonly embeddingId: string;
}

export interface GuidePage {
	readonly ids: readonly string[];
	/** null 代表权威数据源确认到尾，而不是返回数量不足。 */
	readonly next: string | null;
}

export interface GuideChunkMetadata {
	readonly id: string;
	readonly documentVersionId: string;
	readonly entityId: string;
	readonly guideKind: "team";
	readonly gameVersions: { readonly from: string; readonly through: string };
	readonly applicability:
		| { readonly kind: "unrestricted" }
		| { readonly kind: "unknown" }
		| {
				readonly kind: "known";
				readonly conditions: readonly string[];
				readonly requiredCharacterIds: readonly string[];
				readonly requiredEquipmentIds: readonly string[];
		  };
	readonly published: boolean;
	readonly indexReady: boolean;
	readonly title: string;
	readonly sourceUrl: string;
	readonly location: string;
	readonly preview: string;
}

export interface GuideWindow {
	readonly chunk: GuideChunkMetadata;
	readonly body: string;
	readonly complete: boolean;
}

export interface GuideRepository {
	snapshot(filter: GuideFilter, signal: AbortSignal): Promise<GuideSnapshot>;
	keywords(
		filter: GuideFilter,
		snapshot: GuideSnapshot,
		query: string,
		cursor: string | null,
		limit: number,
		signal: AbortSignal,
	): Promise<GuidePage>;
	/** 该快照范围内已发布且索引就绪的分块清单，用于补全向量覆盖。 */
	eligibleIds(
		filter: GuideFilter,
		snapshot: GuideSnapshot,
		cursor: string | null,
		limit: number,
		signal: AbortSignal,
	): Promise<GuidePage>;
	metadata(
		snapshot: GuideSnapshot,
		ids: readonly string[],
		signal: AbortSignal,
	): Promise<readonly GuideChunkMetadata[]>;
	read(
		snapshot: GuideSnapshot,
		chunkId: string,
		signal: AbortSignal,
	): Promise<GuideWindow | null>;
	assertSnapshot(
		filter: GuideFilter,
		snapshot: GuideSnapshot,
		signal: AbortSignal,
	): Promise<void>;
}

export interface EmbeddingPort {
	embed(
		query: string,
		embeddingId: string,
		signal: AbortSignal,
	): Promise<readonly number[]>;
}
export interface VectorIndexPort {
	nearest(
		embeddingId: string,
		vector: readonly number[],
		limit: number,
		signal: AbortSignal,
	): Promise<readonly { readonly id: string; readonly score: number }[]>;
	vectors(
		embeddingId: string,
		ids: readonly string[],
		signal: AbortSignal,
	): Promise<ReadonlyMap<string, readonly number[]>>;
}
export interface GuideData {
	readonly repository: GuideRepository;
	readonly embeddings: EmbeddingPort;
	readonly vectorIndex: VectorIndexPort;
}
