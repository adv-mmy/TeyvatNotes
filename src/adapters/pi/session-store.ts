import { randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { type FileHandle, lstat, mkdir, open, rename } from "node:fs/promises";
import { basename, join } from "node:path";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import type { JsonObject } from "../../application/ports/agent.js";
import {
	type CommittedTurn,
	type ConfirmedConditionUpdate,
	type OpenSession,
	type PendingClarification,
	SessionError,
	type SessionHistory,
	type SessionStore,
	type TurnOutcome,
	type VerifiedTurn,
	type WritableSession,
} from "../../application/ports/session.js";
import { isSupplementGap } from "../../domain/answer-validation.js";
import { isScopeDraft } from "../../domain/query-scope.js";

const uuid =
	/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const noFollow = constants.O_NOFOLLOW ?? 0;
type PendingTurn = { turnId: string; prompt: string; piFile: string };
type PiTrace = { file: string; entryIds: readonly string[] };
type JournalEvent =
	| ({ type: "turn_started" } & PendingTurn)
	| ({ type: "conditions_confirmed" } & ConfirmedConditionUpdate)
	| { type: "clarification_abandoned"; turnId: string }
	| {
			type: "turn_committed";
			turnId: string;
			outcome: TurnOutcome;
			clarification?: PendingClarification | null;
			pi: PiTrace | null;
	  };

function errno(error: unknown, code: string) {
	return error instanceof Error && "code" in error && error.code === code;
}

function normalizedId(value: string): string {
	const id = value.toLowerCase();
	if (!uuid.test(id)) throw new SessionError("SESSION_ID_INVALID");
	return id;
}

function empty(sessionId: string): SessionHistory {
	return { sessionId, conditions: {}, turns: [] };
}

function object(value: unknown): value is Record<string, unknown> {
	return value !== null && typeof value === "object" && !Array.isArray(value);
}

function nonempty(value: unknown): value is string {
	return typeof value === "string" && value.trim().length > 0;
}

function safeReference(value: unknown): boolean {
	if (!nonempty(value)) return false;
	try {
		const url = new URL(value);
		return url.protocol === "https:" && !url.username && !url.password;
	} catch {
		return false;
	}
}

function validCitations(citations: unknown, caution: unknown): boolean {
	return (
		Array.isArray(citations) &&
		citations.length > 0 &&
		citations.every(
			(item: unknown) =>
				object(item) &&
				nonempty(item.source) &&
				(item.version === null || nonempty(item.version)) &&
				(item.url === undefined || safeReference(item.url)) &&
				(item.documentVersionId === undefined ||
					nonempty(item.documentVersionId)) &&
				nonempty(item.location) &&
				nonempty(item.excerpt),
		) &&
		(citations.some((item) => item.version === null)
			? caution === true
			: caution === undefined)
	);
}

function validOutcome(value: unknown): value is TurnOutcome {
	if (!object(value)) return false;
	switch (value.status) {
		case "answered":
			return (
				nonempty(value.text) &&
				validCitations(value.citations, value.versionCaution)
			);
		case "needs_clarification":
			return (
				nonempty(value.text) &&
				(value.referencePrompt === undefined || value.referencePrompt === true)
			);
		case "insufficient_evidence":
			return (
				nonempty(value.text) &&
				(value.citations === undefined
					? value.versionCaution === undefined &&
						(value.supplementGap === undefined ||
							isSupplementGap(value.supplementGap))
					: value.supplementGap === undefined &&
						validCitations(value.citations, value.versionCaution))
			);
		case "failed":
			return (
				typeof value.code === "string" && /^[A-Z_]{1,80}$/.test(value.code)
			);
		case "cancelled":
		case "interrupted":
			return !("text" in value) && !("citations" in value);
		default:
			return false;
	}
}

const scopeFields = new Set([
	"entityId",
	"gameVersion",
	"goal",
	"teamMode",
	"holding",
	"teammates",
	"equipment",
	"inventoryFacts",
]);

function validClarification(value: unknown): value is PendingClarification {
	return (
		object(value) &&
		typeof value.questionTurnId === "string" &&
		uuid.test(value.questionTurnId) &&
		nonempty(value.prompt) &&
		nonempty(value.text) &&
		(value.state === "waiting" || value.state === "ready") &&
		(value.scope === undefined || isScopeDraft(value.scope)) &&
		(value.equipmentId === undefined || nonempty(value.equipmentId)) &&
		(value.versionReference === undefined ||
			((value.versionReference === "offer" ||
				value.versionReference === "accepted") &&
				isScopeDraft(value.scope) &&
				!!value.scope.gameVersion &&
				(value.versionReference !== "offer" || value.state === "waiting"))) &&
		(value.fields === undefined ||
			(Array.isArray(value.fields) &&
				value.fields.every(
					(field: unknown) =>
						typeof field === "string" && scopeFields.has(field),
				)))
	);
}

function parseJournal(sessionId: string, raw: string) {
	let active: PendingTurn | undefined;
	let conditions: JsonObject = {};
	let clarification: PendingClarification | null | undefined;
	const turns: CommittedTurn[] = [];
	const seen = new Set<string>();
	const lines = raw.split("\n");
	let damaged = lines.pop() !== "";
	for (const line of lines) {
		try {
			const item: unknown = JSON.parse(line);
			if (
				!object(item) ||
				item.sessionId !== sessionId ||
				typeof item.at !== "string" ||
				!Number.isFinite(Date.parse(item.at)) ||
				typeof item.turnId !== "string" ||
				!uuid.test(item.turnId)
			)
				throw new Error();
			if (item.type === "turn_started") {
				if (
					active ||
					seen.has(item.turnId) ||
					!nonempty(item.prompt) ||
					typeof item.piFile !== "string" ||
					!/^[0-9T:Z-]+_[0-9a-f-]+\.jsonl$/.test(item.piFile) ||
					!item.piFile.endsWith(`_${item.turnId}.jsonl`)
				)
					throw new Error();
				active = {
					turnId: item.turnId,
					prompt: item.prompt,
					piFile: item.piFile,
				};
				seen.add(item.turnId);
			} else if (
				item.type === "conditions_confirmed" &&
				active?.turnId === item.turnId
			) {
				if (
					!object(item.conditions) ||
					(item.clarification !== undefined &&
						(!validClarification(item.clarification) ||
							item.clarification.questionTurnId !==
								clarification?.questionTurnId))
				)
					throw new Error();
				conditions = { ...conditions, ...(item.conditions as JsonObject) };
				if (item.clarification !== undefined)
					clarification = item.clarification;
			} else if (
				item.type === "clarification_abandoned" &&
				!active &&
				clarification?.questionTurnId === item.turnId
			) {
				clarification = null;
			} else if (
				item.type === "turn_committed" &&
				active?.turnId === item.turnId
			) {
				if (
					!validOutcome(item.outcome) ||
					(item.clarification !== undefined &&
						item.clarification !== null &&
						(!validClarification(item.clarification) ||
							item.outcome.status !== "needs_clarification" ||
							(item.clarification.questionTurnId !== item.turnId &&
								item.clarification.questionTurnId !==
									clarification?.questionTurnId))) ||
					(item.pi !== null &&
						(!object(item.pi) ||
							item.pi.file !== active.piFile ||
							!Array.isArray(item.pi.entryIds) ||
							!item.pi.entryIds.every((id: unknown) => typeof id === "string")))
				)
					throw new Error();
				if (item.clarification !== undefined)
					clarification = item.clarification;
				turns.push({
					turnId: active.turnId,
					prompt: active.prompt,
					outcome: item.outcome,
				});
				active = undefined;
			} else throw new Error();
		} catch {
			damaged = true;
			break;
		}
	}
	return {
		history: {
			sessionId,
			conditions,
			turns,
			...(clarification !== undefined ? { clarification } : {}),
		},
		active,
		damaged,
	};
}

async function readText(path: string): Promise<string> {
	const handle = await open(path, constants.O_RDONLY | noFollow);
	try {
		if (!(await handle.stat()).isFile())
			throw new SessionError("SESSION_CORRUPT");
		return await handle.readFile("utf8");
	} finally {
		await handle.close();
	}
}

async function syncPath(path: string) {
	const handle = await open(path, constants.O_RDONLY | noFollow);
	try {
		await handle.sync();
	} finally {
		await handle.close();
	}
}

async function append(
	directory: string,
	sessionId: string,
	event: JournalEvent,
) {
	const handle = await open(
		join(directory, "turns.jsonl"),
		constants.O_WRONLY | constants.O_APPEND | noFollow,
	);
	try {
		await handle.writeFile(
			`${JSON.stringify({ sessionId, at: new Date().toISOString(), ...event })}\n`,
			"utf8",
		);
		await handle.sync();
	} finally {
		await handle.close();
	}
}

async function recoverTrace(
	directory: string,
	active: PendingTurn,
): Promise<PiTrace | null> {
	const path = join(directory, active.piFile);
	let lines: string[];
	try {
		lines = (await readText(path)).split("\n");
	} catch (error) {
		if (errno(error, "ENOENT")) return null;
		throw error;
	}
	lines.pop(); // 停电时 Pi 最后一行也可能不完整，只关联已完整写入的条目。
	const entryIds: string[] = [];
	for (const [index, line] of lines.entries()) {
		let item: unknown;
		try {
			item = JSON.parse(line);
		} catch {
			break;
		}
		if (!object(item) || !nonempty(item.id)) break;
		if (index === 0) {
			if (item.type !== "session" || item.id !== active.turnId) return null;
		} else entryIds.push(item.id);
	}
	if (!entryIds.length) return null;
	await syncPath(path);
	return { file: active.piFile, entryIds };
}

async function publish(directory: string, history: SessionHistory) {
	const temporary = join(directory, `history-${randomUUID()}.tmp`);
	const handle = await open(temporary, "wx", 0o600);
	try {
		await handle.writeFile(`${JSON.stringify(history)}\n`, "utf8");
		await handle.sync();
	} finally {
		await handle.close();
	}
	await rename(temporary, join(directory, "history.json"));
	await syncPath(directory);
}

async function exclusiveLock(path: string): Promise<FileHandle | undefined> {
	if (process.platform !== "linux") throw new SessionError("SESSION_FAILED");
	const koffi = await import("koffi");
	const libc = koffi.load("libc.so.6");
	try {
		const flock = libc.func("int flock(int fd, int operation)");
		const handle = await open(
			path,
			constants.O_RDWR | constants.O_CREAT | noFollow,
			0o600,
		);
		try {
			if (!(await handle.stat()).isFile())
				throw new SessionError("SESSION_CORRUPT");
			// LOCK_EX | LOCK_NB. EAGAIN 表示其他文件描述符已持锁。
			if (flock(handle.fd, 2 | 4) === 0) return handle;
			if (koffi.errno() !== 11) throw new SessionError("SESSION_FAILED");
		} catch (error) {
			await handle.close();
			throw error;
		}
		await handle.close();
		return undefined;
	} finally {
		libc.unload();
	}
}

class ActiveSession implements WritableSession {
	private active: PendingTurn | undefined;
	private usable = true;
	private closed = false;
	private writing = false;

	constructor(
		private readonly directory: string,
		private readonly lock: FileHandle,
		private view: SessionHistory,
		private readonly reservePi: (turnId: string, directory: string) => string,
		private readonly finishPi: (
			turnId: string,
		) => Promise<Extract<JournalEvent, { type: "turn_committed" }>["pi"]>,
		private readonly discardPi: (turnId: string) => void,
	) {}

	get sessionId() {
		return this.view.sessionId;
	}

	history(): SessionHistory {
		return structuredClone(this.view);
	}

	async begin(prompt: string) {
		const turnId = randomUUID();
		if (this.active || !nonempty(prompt))
			throw new SessionError("SESSION_FAILED");
		await this.write(async () => {
			const piFile = this.reservePi(turnId, this.directory);
			this.active = { turnId, prompt, piFile };
			await append(this.directory, this.sessionId, {
				type: "turn_started",
				...this.active,
			});
		});
		return turnId;
	}

	async appendConfirmedConditions({
		turnId,
		conditions,
		clarification,
	}: ConfirmedConditionUpdate) {
		if (
			this.active?.turnId !== turnId ||
			(clarification &&
				(!validClarification(clarification) ||
					clarification.questionTurnId !==
						this.view.clarification?.questionTurnId))
		)
			throw new SessionError("SESSION_FAILED");
		const confirmed = structuredClone(conditions);
		const progress = clarification
			? { clarification: structuredClone(clarification) }
			: {};
		await this.write(async () => {
			await append(this.directory, this.sessionId, {
				type: "conditions_confirmed",
				turnId,
				conditions: confirmed,
				...progress,
			});
			this.view = {
				...this.view,
				conditions: { ...this.view.conditions, ...confirmed },
				...progress,
			};
		});
	}

	async abandonClarification(questionTurnId: string): Promise<void> {
		if (
			this.active ||
			this.view.clarification?.questionTurnId !== questionTurnId
		)
			throw new SessionError("SESSION_FAILED");
		await this.write(async () => {
			await append(this.directory, this.sessionId, {
				type: "clarification_abandoned",
				turnId: questionTurnId,
			});
			const view = { ...this.view, clarification: null };
			await publish(this.directory, view);
			this.view = view;
		});
	}

	async commit({
		turnId,
		outcome,
		clarification,
	}: VerifiedTurn): Promise<CommittedTurn> {
		const active = this.active;
		if (
			active?.turnId !== turnId ||
			!validOutcome(outcome) ||
			(clarification &&
				(!validClarification(clarification) ||
					outcome.status !== "needs_clarification" ||
					(clarification.questionTurnId !== turnId &&
						clarification.questionTurnId !==
							this.view.clarification?.questionTurnId)))
		)
			throw new SessionError("SESSION_FAILED");
		const verified = structuredClone(outcome);
		const progress =
			clarification === undefined
				? {}
				: { clarification: structuredClone(clarification) };
		return await this.write(async () => {
			const pi = await this.finishPi(turnId);
			await append(this.directory, this.sessionId, {
				type: "turn_committed",
				turnId,
				outcome: verified,
				pi,
				...progress,
			});
			const committed = { turnId, prompt: active.prompt, outcome: verified };
			const view = {
				...this.view,
				turns: [...this.view.turns, committed],
				...progress,
			};
			this.active = undefined;
			await publish(this.directory, view);
			this.view = view;
			return structuredClone(committed);
		});
	}

	private async write<T>(action: () => Promise<T>): Promise<T> {
		if (!this.usable || this.closed || this.writing)
			throw new SessionError("SESSION_FAILED");
		this.writing = true;
		try {
			return await action();
		} catch {
			this.usable = false;
			throw new SessionError("SESSION_FAILED");
		} finally {
			this.writing = false;
		}
	}

	async close() {
		if (this.closed) return;
		if (this.writing) throw new SessionError("SESSION_FAILED");
		this.closed = true;
		if (this.active) this.discardPi(this.active.turnId);
		await this.lock.close();
	}
}

export class PiSessionStore implements SessionStore {
	private readonly turns = new Map<
		string,
		{ manager: SessionManager; claimed: boolean }
	>();

	constructor(
		private readonly root: string,
		private readonly cwd: string,
	) {}

	managerFor(turnId: string): SessionManager {
		const active = this.turns.get(turnId);
		if (!active || active.claimed) throw new SessionError("SESSION_FAILED");
		active.claimed = true;
		return active.manager;
	}

	async create(): Promise<WritableSession> {
		await mkdir(this.root, { recursive: true, mode: 0o700 });
		const sessionId = randomUUID();
		const directory = join(this.root, sessionId);
		await mkdir(directory, { mode: 0o700 });
		const lock = await exclusiveLock(join(directory, "writer.lock"));
		if (!lock) throw new SessionError("SESSION_FAILED");
		try {
			const journal = await open(join(directory, "turns.jsonl"), "wx", 0o600);
			try {
				await journal.sync();
			} finally {
				await journal.close();
			}
			const history = empty(sessionId);
			await publish(directory, history);
			await syncPath(this.root);
			return this.writer(directory, lock, history);
		} catch (error) {
			await lock.close();
			throw error;
		}
	}

	async open(value: string): Promise<OpenSession> {
		const sessionId = normalizedId(value);
		const directory = await this.existingDirectory(sessionId);
		const lock = await exclusiveLock(join(directory, "writer.lock"));
		if (!lock) return { mode: "read", sessionId, code: "SESSION_BUSY" };
		try {
			const { history, active, damaged } = parseJournal(
				sessionId,
				await readText(join(directory, "turns.jsonl")),
			);
			if (active) {
				const outcome = { status: "interrupted" } as const;
				history.turns.push({
					turnId: active.turnId,
					prompt: active.prompt,
					outcome,
				});
				if (!damaged)
					await append(directory, sessionId, {
						type: "turn_committed",
						turnId: active.turnId,
						outcome,
						pi: await recoverTrace(directory, active),
					});
			}
			if (damaged) {
				await lock.close();
				return { mode: "read", sessionId, code: "SESSION_CORRUPT", history };
			}
			await publish(directory, history);
			return { mode: "write", session: this.writer(directory, lock, history) };
		} catch (error) {
			await lock.close();
			throw error;
		}
	}

	async readHistory(value: string): Promise<SessionHistory> {
		const sessionId = normalizedId(value);
		const directory = await this.existingDirectory(sessionId);
		try {
			const snapshot: SessionHistory = JSON.parse(
				await readText(join(directory, "history.json")),
			);
			if (
				snapshot.sessionId !== sessionId ||
				!object(snapshot.conditions) ||
				!Array.isArray(snapshot.turns) ||
				(snapshot.clarification !== undefined &&
					snapshot.clarification !== null &&
					!validClarification(snapshot.clarification)) ||
				!snapshot.turns.every(
					(item) =>
						object(item) &&
						typeof item.turnId === "string" &&
						uuid.test(item.turnId) &&
						nonempty(item.prompt) &&
						validOutcome(item.outcome),
				)
			)
				throw new Error();
			return snapshot;
		} catch (error) {
			if (errno(error, "ENOENT")) return empty(sessionId);
			throw new SessionError("SESSION_CORRUPT");
		}
	}

	private async existingDirectory(sessionId: string) {
		const directory = join(this.root, sessionId);
		try {
			if (!(await lstat(directory)).isDirectory())
				throw new SessionError("SESSION_NOT_FOUND");
			return directory;
		} catch (error) {
			if (errno(error, "ENOENT")) throw new SessionError("SESSION_NOT_FOUND");
			throw error;
		}
	}

	private writer(directory: string, lock: FileHandle, history: SessionHistory) {
		return new ActiveSession(
			directory,
			lock,
			history,
			(turnId, dir) => {
				const manager = SessionManager.create(this.cwd, dir, { id: turnId });
				const file = manager.getSessionFile();
				if (!file) throw new SessionError("SESSION_FAILED");
				this.turns.set(turnId, { manager, claimed: false });
				return basename(file);
			},
			async (turnId) => {
				const manager = this.turns.get(turnId)?.manager;
				this.turns.delete(turnId);
				const file = manager?.getSessionFile();
				const entries = manager?.getEntries() ?? [];
				if (
					!file ||
					!entries.some(
						(entry) =>
							entry.type === "message" && entry.message.role === "user",
					)
				)
					return null;
				await syncPath(file);
				await syncPath(directory);
				return {
					file: basename(file),
					entryIds: entries.map((entry) => entry.id),
				};
			},
			(turnId) => {
				this.turns.delete(turnId);
			},
		);
	}
}
