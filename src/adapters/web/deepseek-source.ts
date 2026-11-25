import {
	type GuideSource,
	permittedUrl,
	type SourcePermit,
} from "../../application/ports/guide-source.js";

export class DeepSeekSource implements GuideSource {
	constructor(private readonly key: string) {}
	// redirect:error 只能保护到 DeepSeek 的请求，不能限制 DeepSeek 读取目标页时的跳转。
	canFetch(_permit: SourcePermit): boolean {
		return false;
	}
	private async post(
		route: "search" | "fetch",
		payload: object,
		signal: AbortSignal,
	) {
		signal.throwIfAborted();
		const response = await fetch(`https://api.deepseek.com/v1/tools/${route}`, {
			method: "POST",
			redirect: "error",
			signal,
			headers: {
				"Content-Type": "application/json",
				Authorization: `Bearer ${this.key}`,
			},
			body: JSON.stringify(payload),
		});
		if (!response.ok || !response.body) {
			await response.body?.cancel();
			throw new Error("WEB_SOURCE_UNAVAILABLE");
		}
		const reader = response.body.getReader();
		const chunks: Uint8Array[] = [];
		let size = 0;
		try {
			for (;;) {
				const { value, done } = await reader.read();
				if (done) break;
				size += value.byteLength;
				if (size > 1_000_000) {
					await reader.cancel();
					throw new Error("WEB_SOURCE_UNAVAILABLE");
				}
				chunks.push(value);
			}
		} finally {
			reader.releaseLock();
		}
		return JSON.parse(Buffer.concat(chunks).toString("utf8"));
	}
	async search(query: string, signal: AbortSignal): Promise<string[]> {
		const data = await this.post(
			"search",
			{ query, count: 10, need_content: false },
			signal,
		);
		if (!Array.isArray(data?.results))
			throw new Error("WEB_SOURCE_UNAVAILABLE");
		return data.results.map((r: { url?: unknown }) => {
			if (typeof r?.url !== "string") throw new Error("WEB_SOURCE_UNAVAILABLE");
			return r.url;
		});
	}
	async fetch(url: string, permit: SourcePermit, signal: AbortSignal) {
		if (!this.canFetch(permit) || permittedUrl(url, permit) !== url)
			throw new Error("SOURCE_RESTRICTED");
		const data = await this.post("fetch", { url }, signal);
		if (typeof data?.title !== "string" || typeof data.content !== "string")
			throw new Error("WEB_SOURCE_UNAVAILABLE");
		return { title: data.title as string, content: data.content as string };
	}
}
