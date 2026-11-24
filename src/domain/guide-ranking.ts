/** 每路独立按首次出现的分块计名次；同一路的重复项不重复累积分数。 */
export function rankGuides(
	channels: readonly (readonly string[])[],
	k = 60,
): readonly { readonly id: string; readonly score: number }[] {
	if (!Number.isSafeInteger(k) || k < 1) throw new RangeError("无效的 RRF k");
	const scores = new Map<string, number>();
	for (const channel of channels) {
		let rank = 0;
		for (const id of new Set(channel)) {
			rank++;
			scores.set(id, (scores.get(id) ?? 0) + 1 / (k + rank));
		}
	}
	return [...scores]
		.map(([id, score]) => ({ id, score }))
		.sort(
			(a, b) => b.score - a.score || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
		);
}
