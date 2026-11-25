import type { Pool, QueryResultRow } from "pg";
import { untilAborted } from "../../application/ports/agent.js";
import {
	type EmbeddingConfig,
	type GuideChunkMetadata,
	type GuideFilter,
	type GuidePage,
	type GuideRepository,
	type GuideSnapshot,
	GuideSnapshotChanged,
	type GuideWriteRepository,
	type ImportDocument,
	type StoredGuide,
} from "../../application/ports/guide-data.js";
import { gameVersionKey } from "../../domain/query-scope.js";
import { transaction } from "./migrate.js";

const canonical = (text: string) => text.normalize("NFKC").toLowerCase();
const visible = `FROM guide_chunk c
	JOIN guide_version v ON v.id = c.version_id
	JOIN guide_index_checkpoint i ON i.version_id = v.id
	WHERE i.embedding_id = $1 AND i.ready AND v.status = 'published'`;
const inScope = `AND v.entity_id = $2 AND v.guide_kind = $3
	AND $4 BETWEEN v.game_from_key AND v.game_through_key`;
const readColumns = `c.id, v.id AS "documentVersionId", v.entity_id AS "entityId",
	v.guide_kind AS "guideKind", jsonb_build_object('from', v.game_from, 'through', v.game_through) AS "gameVersions",
	v.applicability, true AS published, true AS "indexReady", v.title,
	(SELECT source_url FROM guide_source WHERE id = v.source_id) AS "sourceUrl",
	c.location, left(c.body, 180) AS preview`;

function versionKey(value: string): number {
	const key = gameVersionKey(value);
	if (key === undefined) throw new Error("INVALID_GAME_VERSION");
	return key;
}

function scopeParams(embeddingId: string, filter: GuideFilter) {
	return [
		embeddingId,
		filter.entityId,
		filter.guideKind,
		versionKey(filter.gameVersion),
	];
}

export class PostgreSqlGuideRepository
	implements GuideRepository, GuideWriteRepository
{
	constructor(
		private readonly pool: Pool,
		private readonly embeddingId: string,
	) {}

	private async rows<T extends QueryResultRow>(
		sql: string,
		args: unknown[],
		signal: AbortSignal,
	): Promise<T[]> {
		signal.throwIfAborted();
		const result = await untilAborted(
			() => this.pool.query<T>(sql, args),
			signal,
		);
		signal.throwIfAborted();
		return result.rows;
	}

	async snapshot(
		filter: GuideFilter,
		signal: AbortSignal,
	): Promise<GuideSnapshot> {
		const [row] = await this.rows<{ revision: string; missing: boolean }>(
			`SELECT s.revision::text, EXISTS (
				SELECT 1 FROM guide_version v
				LEFT JOIN guide_index_checkpoint i ON i.version_id = v.id AND i.embedding_id = $1
				WHERE v.status = 'published' ${inScope} AND i.ready IS DISTINCT FROM true
			) AS missing FROM guide_store_state s WHERE s.singleton`,
			scopeParams(this.embeddingId, filter),
			signal,
		);
		if (!row || row.missing) throw new Error("INDEX_NOT_READY");
		return { revision: row.revision, embeddingId: this.embeddingId };
	}

	async assertSnapshot(
		filter: GuideFilter,
		expected: GuideSnapshot,
		signal: AbortSignal,
	): Promise<void> {
		const current = await this.snapshot(filter, signal);
		if (
			current.revision !== expected.revision ||
			current.embeddingId !== expected.embeddingId
		)
			throw new GuideSnapshotChanged();
	}

	async keywords(
		filter: GuideFilter,
		snapshot: GuideSnapshot,
		query: string,
		cursor: string | null,
		limit: number,
		signal: AbortSignal,
	): Promise<GuidePage> {
		const previous = cursor ? /^([0-9]+):([0-9a-f]{64})$/u.exec(cursor) : null;
		if (cursor && !previous) throw new Error("INVALID_CURSOR");
		const keyword = canonical(query).trim();
		const words = [
			...new Set(
				keyword.split(/[\s\p{P}\p{S}]+/u).filter((word) => word.length > 1),
			),
		].slice(0, 24);
		const rows = await this.rows<{ id: string; score: number }>(
			`WITH scored AS (
				SELECT c.id, (
					(SELECT count(*) FROM unnest(c.terms) AS t(term) WHERE strpos($5, t.term) > 0) * 4
					+ (SELECT count(*) FROM unnest($6::text[]) AS w(word) WHERE strpos(c.keyword_body, w.word) > 0)
				)::int AS score ${visible} ${inScope}
			)
			SELECT id, score FROM scored
			WHERE score > 0 AND ($7::int IS NULL OR score < $7 OR (score = $7 AND id > $8))
			ORDER BY score DESC, id ASC LIMIT $9`,
			[
				...scopeParams(snapshot.embeddingId, filter),
				keyword,
				words,
				previous ? Number(previous[1]) : null,
				previous?.[2] ?? null,
				limit + 1,
			],
			signal,
		);
		const last = rows[limit - 1];
		return {
			ids: rows.slice(0, limit).map(({ id }) => id),
			next: rows.length > limit && last ? `${last.score}:${last.id}` : null,
		};
	}

	async eligibleIds(
		filter: GuideFilter,
		snapshot: GuideSnapshot,
		cursor: string | null,
		limit: number,
		signal: AbortSignal,
	): Promise<GuidePage> {
		const rows = await this.rows<{ id: string }>(
			`SELECT c.id ${visible} ${inScope}
			AND ($5::text IS NULL OR c.id > $5) ORDER BY c.id LIMIT $6`,
			[...scopeParams(snapshot.embeddingId, filter), cursor, limit + 1],
			signal,
		);
		const ids = rows.slice(0, limit).map(({ id }) => id);
		return { ids, next: rows.length > limit ? (ids.at(-1) ?? null) : null };
	}

	metadata(
		snapshot: GuideSnapshot,
		ids: readonly string[],
		signal: AbortSignal,
	): Promise<GuideChunkMetadata[]> {
		return this.rows<GuideChunkMetadata>(
			`SELECT ${readColumns} ${visible} AND c.id = ANY($2::text[])`,
			[snapshot.embeddingId, ids],
			signal,
		);
	}

	async read(snapshot: GuideSnapshot, chunkId: string, signal: AbortSignal) {
		const [row] = await this.rows<GuideChunkMetadata & { body: string }>(
			`SELECT ${readColumns}, c.body ${visible} AND c.id = $2`,
			[snapshot.embeddingId, chunkId],
			signal,
		);
		if (!row) return null;
		const { body, ...chunk } = row;
		return { chunk, body, complete: true };
	}

	async archiveIncomplete(
		page: import("../../application/ports/guide-source.js").IncompletePage,
		signal: AbortSignal,
	) {
		await transaction(this.pool, signal, async (client) => {
			await client.query(
				"INSERT INTO guide_incomplete_page(source_url,content_hash,permission_ref,title,content,field_proofs,reasons) VALUES ($1,encode(sha256(convert_to($2,'UTF8')),'hex'),$3,$4,$2,$5,$6) ON CONFLICT DO NOTHING",
				[
					page.url,
					page.content,
					page.permission,
					page.title,
					JSON.stringify(page.proofs),
					page.reasons,
				],
			);
		});
	}

	async save(
		document: ImportDocument,
		embedding: EmbeddingConfig,
		signal: AbortSignal,
	): Promise<StoredGuide> {
		await transaction(this.pool, signal, async (client) => {
			await client.query(
				"INSERT INTO guide_embedding_config (id, model, dimensions) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
				[embedding.id, embedding.model, embedding.dimensions],
			);
			const config = await client.query(
				"SELECT 1 FROM guide_embedding_config WHERE id = $1 AND model = $2 AND dimensions = $3",
				[embedding.id, embedding.model, embedding.dimensions],
			);
			if (!config.rowCount) throw new Error("EMBEDDING_CONFIG_MISMATCH");
			await client.query(
				"INSERT INTO guide_source (source_url, title, source_type, permission_ref) VALUES ($1,$2,$3,$4) ON CONFLICT (source_url) DO NOTHING",
				[
					document.sourceUrl,
					document.title,
					document.webEvidence ? "web" : "manual",
					document.webEvidence?.permission ?? null,
				],
			);
			const { rowCount } = await client.query(
				`INSERT INTO guide_version (id, source_id, title, content, content_hash, processing_version,
					entity_id, guide_kind, game_from, game_through, game_from_key, game_through_key, applicability,web_evidence)
				SELECT $1, id, $3, $4, $5, $6, $7, 'team', $8, $9, $10, $11, $12::jsonb,$13::jsonb
				FROM guide_source WHERE source_url = $2
				ON CONFLICT (source_id, content_hash, processing_version) DO NOTHING`,
				[
					document.id,
					document.sourceUrl,
					document.title,
					document.content,
					document.contentHash,
					document.processingVersion,
					document.entityId,
					document.gameVersions.from,
					document.gameVersions.through,
					versionKey(document.gameVersions.from),
					versionKey(document.gameVersions.through),
					JSON.stringify(document.applicability),
					document.webEvidence ? JSON.stringify(document.webEvidence) : null,
				],
			);
			if (rowCount)
				await client.query(
					`INSERT INTO guide_chunk (id, version_id, ordinal, section, location, body, keyword_body, terms)
					SELECT x.id, $1, x.ordinal, x.section, x.location, x.body, x.keyword_body, x.terms
					FROM jsonb_to_recordset($2::jsonb) AS x(id text, ordinal int, section text,
					location text, body text, keyword_body text, terms text[])`,
					[
						document.id,
						JSON.stringify(
							document.chunks.map((chunk, index) => ({
								...chunk,
								ordinal: index + 1,
								keyword_body: canonical(chunk.body),
							})),
						),
					],
				);
			signal.throwIfAborted();
			await client.query(
				`INSERT INTO guide_index_checkpoint (version_id, embedding_id, expected_ids)
				SELECT version_id, $2, array_agg(id ORDER BY ordinal) FROM guide_chunk
				WHERE version_id = $1 GROUP BY version_id ON CONFLICT DO NOTHING`,
				[document.id, embedding.id],
			);
		});
		const saved = await this.forPublication(document.id, embedding.id, signal);
		if (!saved) throw new Error("GUIDE_NOT_FOUND");
		return saved;
	}

	async forPublication(
		id: string,
		embeddingId: string,
		signal: AbortSignal,
	): Promise<StoredGuide | null> {
		const [row] = await this.rows<StoredGuide & { expected: string[] }>(
			`SELECT v.id, v.entity_id AS "entityId", v.status = 'published' AS published,
				jsonb_build_object('from', v.game_from, 'through', v.game_through) AS "gameVersions",
				i.ready AS indexed, i.expected_ids AS expected,
				jsonb_agg(jsonb_build_object('id', c.id, 'body', c.body) ORDER BY c.ordinal) AS chunks
			FROM guide_version v JOIN guide_index_checkpoint i ON i.version_id = v.id
			JOIN guide_chunk c ON c.version_id = v.id
			WHERE v.id = $1 AND i.embedding_id = $2 AND v.status IN ('pending', 'published')
			GROUP BY v.id, i.ready, i.expected_ids`,
			[id, embeddingId],
			signal,
		);
		if (!row) return null;
		const { expected, ...guide } = row;
		if (
			expected.length !== guide.chunks.length ||
			expected.some((key, index) => key !== guide.chunks[index]?.id)
		)
			throw new Error("INDEX_NOT_READY");
		return guide;
	}

	async finishIndex(
		id: string,
		embeddingId: string,
		signal: AbortSignal,
	): Promise<void> {
		await transaction(this.pool, signal, async (client) => {
			await client.query(
				"SELECT revision FROM guide_store_state WHERE singleton FOR UPDATE",
			);
			const update = await client.query(
				"UPDATE guide_index_checkpoint SET ready = true WHERE version_id = $1 AND embedding_id = $2 AND NOT ready",
				[id, embeddingId],
			);
			if (update.rowCount)
				await client.query(
					`UPDATE guide_store_state SET revision = revision + 1
					WHERE singleton AND EXISTS (SELECT 1 FROM guide_version WHERE id = $1 AND status = 'published')`,
					[id],
				);
		});
	}

	async publish(
		id: string,
		embeddingId: string,
		signal: AbortSignal,
	): Promise<void> {
		await transaction(this.pool, signal, async (client) => {
			await client.query(
				"SELECT revision FROM guide_store_state WHERE singleton FOR UPDATE",
			);
			const result = await client.query<{ status: string }>(
				`SELECT v.status FROM guide_version v JOIN guide_source s ON s.id = v.source_id
				JOIN guide_index_checkpoint i ON i.version_id = v.id
				WHERE v.id = $1 AND i.embedding_id = $2 AND i.ready
				AND (v.processing_version = 'team-1' OR (v.web_evidence IS NOT NULL AND v.web_evidence->>'parser'=v.processing_version))
				AND v.status IN ('pending', 'published') AND v.applicability->>'kind' IN ('known', 'unrestricted')
				FOR UPDATE OF v`,
				[id, embeddingId],
			);
			if (!result.rows[0]) throw new Error("INDEX_NOT_READY");
			if (result.rows[0].status === "pending") {
				await client.query(
					"UPDATE guide_version SET status = 'published' WHERE id = $1",
					[id],
				);
				await client.query(
					"UPDATE guide_store_state SET revision = revision + 1 WHERE singleton",
				);
			}
		});
	}
}
