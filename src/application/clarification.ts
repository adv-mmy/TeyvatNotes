import {
	applySupplement,
	type Holding,
	type PlayerInventory,
	parseQuestion,
	parseSupplement,
	type QueryScope,
	resolveScope,
	type ScopeDraft,
	type ScopeField,
	type TerminologyEntry,
} from "../domain/query-scope.js";
import type { JsonObject } from "./ports/agent.js";
import type {
	PendingClarification,
	SessionHistory,
	TurnOutcome,
	WritableSession,
} from "./ports/session.js";

type Unfinished = Omit<PendingClarification, "questionTurnId">;
type Confirmation = {
	readonly conditions: JsonObject;
	readonly clarification: PendingClarification;
};
export interface DialogueTurn {
	readonly kind: "run";
	readonly prompt: string;
	readonly scope?: QueryScope;
	readonly scopeDraft?: ScopeDraft;
	readonly preflight?: Extract<
		TurnOutcome,
		{ status: "needs_clarification" | "insufficient_evidence" }
	>;
	readonly waiting?: Unfinished;
	readonly continuing?: PendingClarification;
	readonly confirmation?: Confirmation;
}
type Notice = { readonly kind: "notice"; readonly text: string };
type Draft = {
	readonly text: string;
	readonly input?: ScopeDraft;
	readonly updates?: ScopeDraft;
};
type Decision = DialogueTurn | Notice;
const readyText =
	"原问题的条件已补齐。输入「继续」发起新一轮，或用「新问题：…」切换。";

function isNewTeamQuestion(input: string): boolean {
	return (
		!/^(角色|版本|目标|模式|目标角色持有|队友|装备)\s*[：:]/u.test(input) &&
		/怎么配队|如何配队|怎么组队|如何组队|阵容怎么搭/u.test(input)
	);
}

const questions: Readonly<Record<ScopeField, string>> = {
	entityId: "角色：标准名或已登记别名",
	gameVersion: "版本：明确版本号",
	goal: "目标：配队",
	teamMode: "模式：通用 / 按库存",
	holding: "目标角色持有：持有 / 不持有",
	teammates: "队友：名字列表，或「无」",
	equipment: "装备：武器或圣遗物的标准名、已登记别名，或「无」",
};

function playerFrom(history: SessionHistory): PlayerInventory {
	const holdings = history.conditions.holdings;
	const teammates = history.conditions.teammates;
	return {
		holdings: Object.fromEntries(
			holdings && typeof holdings === "object" && !Array.isArray(holdings)
				? Object.entries(holdings).filter(
						(entry): entry is [string, Holding] =>
							entry[1] === "owned" || entry[1] === "not_owned",
					)
				: [],
		),
		...(Array.isArray(teammates) &&
		teammates.every((value): value is string => typeof value === "string")
			? { teammates }
			: {}),
	};
}

function notice(text: string): Notice {
	return { kind: "notice", text };
}

export class Clarification {
	private readonly drafts = new WeakMap<WritableSession, Draft>();

	constructor(private readonly terms?: readonly TerminologyEntry[]) {}

	current(session: WritableSession): string | undefined {
		const pending = session.history().clarification;
		if (!pending) return undefined;
		return pending.state === "waiting"
			? `需要补充条件：${pending.text}\n请直接输入要补充的条件。`
			: readyText;
	}

	discardDraft(session: WritableSession): string | undefined {
		return this.drafts.delete(session)
			? "已放弃待确认的条件。可重新输入。"
			: undefined;
	}

	async prepare(session: WritableSession, line: string): Promise<Decision> {
		const history = session.history();
		const pending = history.clarification;
		const [, newQuestion] = /^新问题\s*[：:]\s*(\S.*)$/u.exec(line) ?? [];
		if (line === "放弃" || newQuestion) {
			this.drafts.delete(session);
			if (pending) await session.abandonClarification(pending.questionTurnId);
			if (newQuestion) return this.fresh(newQuestion, history);
			return notice(pending ? "已放弃当前追问。" : "没有待处理的追问。");
		}
		if (pending?.state === "ready" && line === "继续")
			return pending.scope
				? this.evaluate(
						pending.prompt,
						pending.scope,
						playerFrom(history),
						pending,
					)
				: { kind: "run", prompt: pending.prompt, continuing: pending };
		if (pending?.state !== "waiting") {
			if (pending?.state === "ready" && pending.scope) {
				// 有明确新提问意图时切换；普通输入不会被擅自用于重试。
				if (isNewTeamQuestion(line)) {
					await session.abandonClarification(pending.questionTurnId);
					return this.fresh(line, history);
				}
				return notice(readyText);
			}
			return this.fresh(line, history);
		}

		if (line === "取消")
			return notice(
				this.discardDraft(session) ??
					"没有待确认的条件。可继续补充，或输入「放弃」。",
			);
		if (line === "确认") {
			const draft = this.drafts.get(session);
			if (!draft) return notice("没有待确认的条件。请先输入要补充的条件。");
			this.drafts.delete(session);
			if (!draft.input || !draft.updates) {
				return {
					kind: "run",
					prompt: draft.text,
					continuing: pending,
					confirmation: {
						conditions: { clarification: draft.text },
						clarification: { ...pending, state: "ready" },
					},
				};
			}
			const player = playerFrom(history);
			const holdings =
				draft.updates.holding && draft.input.entityId
					? {
							...player.holdings,
							[draft.input.entityId]: draft.updates.holding,
						}
					: undefined;
			const confirmed: JsonObject = {
				...(holdings ? { holdings } : {}),
				...(draft.updates.teammates
					? { teammates: draft.updates.teammates }
					: {}),
			};
			const next = this.evaluate(
				pending.prompt,
				draft.input,
				{
					...player,
					...(holdings ? { holdings } : {}),
					...(draft.updates.teammates
						? { teammates: draft.updates.teammates }
						: {}),
				},
				pending,
			);
			const clarification: PendingClarification = next.waiting
				? { ...next.waiting, questionTurnId: pending.questionTurnId }
				: {
						questionTurnId: pending.questionTurnId,
						prompt: pending.prompt,
						state: "ready",
						text: readyText,
						scope: draft.input,
					};
			return {
				...next,
				confirmation: { conditions: confirmed, clarification },
			};
		}
		if (pending.scope && this.terms) {
			if (isNewTeamQuestion(line)) {
				this.drafts.delete(session);
				await session.abandonClarification(pending.questionTurnId);
				return this.fresh(line, history);
			}
			const parsed = parseSupplement(line, this.terms, pending.fields ?? []);
			if (!parsed)
				return notice(`未能明确识别补充条件，未保存。\n${pending.text}`);
			this.drafts.set(session, {
				text: line,
				input: applySupplement(pending.scope, parsed.patch),
				updates: parsed.patch,
			});
			return notice(
				`补充条件待确认：${parsed.changes.join("；")}。输入「确认」保存并继续，输入「取消」放弃草稿，或重新输入替换。`,
			);
		}
		this.drafts.set(session, { text: line });
		return notice(
			"补充条件待确认。输入「确认」保存并继续，输入「取消」放弃，或重新输入替换。",
		);
	}

	finish(
		turn: DialogueTurn,
		turnId: string,
		outcome: TurnOutcome,
		equipmentId?: string,
	): PendingClarification | null | undefined {
		if (outcome.status === "needs_clarification") {
			if (equipmentId && turn.scopeDraft)
				return {
					questionTurnId: turn.continuing?.questionTurnId ?? turnId,
					prompt: turn.continuing?.prompt ?? turn.prompt,
					text: outcome.text,
					state: "waiting",
					scope: { ...turn.scopeDraft, equipmentRequired: true },
					fields: ["equipment"],
					equipmentId,
				};
			if (turn.waiting)
				return {
					...turn.waiting,
					questionTurnId: turn.continuing?.questionTurnId ?? turnId,
				};
			// 不带攻略范围的运行时也可提出通用追问；同样使用应用层会话状态。
			if (!turn.scope)
				return {
					questionTurnId: turn.continuing?.questionTurnId ?? turnId,
					prompt: turn.continuing?.prompt ?? turn.prompt,
					text: outcome.text,
					state: "waiting",
				};
		}
		if (
			(outcome.status === "answered" ||
				outcome.status === "insufficient_evidence") &&
			turn.continuing
		)
			return null;
		return undefined; // 失败或中断不清除已确认的上下文。
	}

	private fresh(prompt: string, history: SessionHistory): DialogueTurn {
		return this.terms
			? this.evaluate(
					prompt,
					parseQuestion(prompt, this.terms),
					playerFrom(history),
				)
			: { kind: "run", prompt };
	}

	private evaluate(
		prompt: string,
		scope: ScopeDraft,
		player: PlayerInventory,
		continuing?: PendingClarification,
	): DialogueTurn {
		const result = resolveScope(scope, player);
		const turn = {
			kind: "run",
			prompt,
			...(continuing ? { continuing } : {}),
		} as const;
		switch (result.kind) {
			case "ready":
				return { ...turn, scope: result.scope, scopeDraft: scope };
			case "no_team":
				return {
					...turn,
					preflight: {
						status: "insufficient_evidence",
						text:
							result.reason === "target_absent"
								? "已确认未持有目标角色，无法按库存提供该角色的配队。"
								: "已确认没有可用的队友，无法按库存提供配队。",
					},
				};
			case "missing": {
				const text = `请补充以下条件：${result.fields.map((field) => questions[field]).join("；")}。`;
				return {
					...turn,
					preflight: { status: "needs_clarification", text },
					waiting: {
						prompt,
						text,
						state: "waiting",
						scope,
						fields: result.fields,
					},
				};
			}
		}
	}
}
