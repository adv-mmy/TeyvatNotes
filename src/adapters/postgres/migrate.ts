import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import type { Pool, PoolClient } from "pg";

export async function transaction<T>(
	pool: Pool,
	signal: AbortSignal,
	work: (client: PoolClient) => Promise<T>,
): Promise<T> {
	signal.throwIfAborted();
	const client = await pool.connect();
	let reusable = true;
	try {
		signal.throwIfAborted();
		await client.query("BEGIN");
		const result = await work(client);
		signal.throwIfAborted();
		await client.query("COMMIT");
		return result;
	} catch (error) {
		try {
			await client.query("ROLLBACK");
		} catch {
			reusable = false;
		}
		throw error;
	} finally {
		client.release(!reusable);
	}
}

const names = ["001_guide_store", "002_web_guide_evidence"];

export async function migrate(
	pool: Pool,
	signal: AbortSignal,
): Promise<boolean> {
	const scripts = await Promise.all(
		names.map(async (name) => {
			const location = new URL(
				`../../../../migrations/${name}.sql`,
				import.meta.url,
			);
			const sql = await readFile(location, { encoding: "utf8", signal });
			return {
				name,
				sql,
				checksum: createHash("sha256").update(sql).digest("hex"),
			};
		}),
	);
	return transaction(pool, signal, async (client) => {
		await client.query("SELECT pg_advisory_xact_lock(1661107754, 1)");
		await client.query(`CREATE TABLE IF NOT EXISTS guide_schema_migration (
		    id text PRIMARY KEY, checksum text NOT NULL
		)`);
		let changed = false;
		for (const { name, sql, checksum } of scripts) {
			const { rows } = await client.query<{ checksum: string }>(
				"SELECT checksum FROM guide_schema_migration WHERE id = $1",
				[name],
			);
			if (rows[0]) {
				if (rows[0].checksum !== checksum)
					throw new Error("MIGRATION_CHECKSUM_MISMATCH");
				continue;
			}
			signal.throwIfAborted();
			await client.query(sql);
			await client.query(
				"INSERT INTO guide_schema_migration(id, checksum) VALUES ($1, $2)",
				[name, checksum],
			);
			changed = true;
		}
		return changed;
	});
}
