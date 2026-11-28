import type { GameVersions } from "../../domain/query-scope.js";

/** 仅传入可安全前置筛选的范围；null 只查版本未知层。 */
export interface GuideFilter {
	readonly entityId: string;
	readonly gameVersion: string | null;
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
	readonly gameVersions: GameVersions;
	readonly fromWeb?: boolean;
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
	/** 只核对指定分块在当前集合中的存在性，不传回向量或参与排名。 */
	hasAll(
		embeddingId: string,
		ids: readonly string[],
		signal: AbortSignal,
	): Promise<boolean>;
	nearest(
		embeddingId: string,
		vector: readonly number[],
		limit: number,
		signal: AbortSignal,
		filter?: GuideFilter,
	): Promise<readonly { readonly id: string; readonly score: number }[]>;
	vectors(
		embeddingId: string,
		ids: readonly string[],
		signal: AbortSignal,
	): Promise<ReadonlyMap<string, readonly number[]>>;
}

export interface EmbeddingConfig {
	readonly id: string;
	readonly model: string;
	readonly dimensions: number;
}

export interface ImportedChunk {
	readonly id: string;
	readonly section: string;
	readonly location: string;
	readonly body: string;
	readonly terms: readonly string[];
}

export interface ImportDocument {
	readonly webEvidence?: {
		readonly permission: string;
		readonly parser: string;
		readonly fields: import("./guide-source.js").FieldProofs;
		readonly revision: import("./guide-source.js").SourceRevision;
		readonly unknownGameVersionReason?: "not_stated";
	};
	readonly id: string;
	readonly title: string;
	readonly sourceUrl: string;
	readonly content: string;
	readonly contentHash: string;
	readonly processingVersion: string;
	readonly entityId: string;
	readonly gameVersions: GuideChunkMetadata["gameVersions"];
	readonly applicability: GuideChunkMetadata["applicability"];
	readonly chunks: readonly ImportedChunk[];
}

export interface StoredGuide {
	readonly id: string;
	readonly entityId: string;
	readonly gameVersions: GuideChunkMetadata["gameVersions"];
	readonly published: boolean;
	readonly indexed: boolean;
	readonly chunks: readonly { readonly id: string; readonly body: string }[];
}

export interface GuideWriteRepository {
	save(
		document: ImportDocument,
		embedding: EmbeddingConfig,
		signal: AbortSignal,
	): Promise<StoredGuide>;
	forPublication(
		id: string,
		embeddingId: string,
		signal: AbortSignal,
	): Promise<StoredGuide | null>;
	finishIndex(
		id: string,
		embeddingId: string,
		signal: AbortSignal,
	): Promise<void>;
	publish(id: string, embeddingId: string, signal: AbortSignal): Promise<void>;
}

export interface VectorWritePort extends VectorIndexPort {
	upsert(
		embeddingId: string,
		guide: StoredGuide,
		vectors: ReadonlyMap<string, readonly number[]>,
		signal: AbortSignal,
	): Promise<void>;
}
export interface GuideData {
	readonly repository: GuideRepository;
	readonly embeddings: EmbeddingPort;
	readonly vectorIndex: VectorIndexPort;
}
