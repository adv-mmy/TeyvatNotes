"""固定词面特征哈希：仅验证向量管道，不是 GPT 或语义 Embedding。"""

import asyncio
import hashlib
import unicodedata

from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.domain.rag import EmbeddingProfile, unit_vector


class DemoLexicalEmbedding:
    """无网络、无密钥、无训练权重；只允许 fixture 范围。"""

    @property
    def profile(self) -> EmbeddingProfile:
        return EmbeddingProfile(
            "local_demo", "char-ngram-sha256", "section-text-nfkc-v1", 256, True, False
        )

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if (
            not isinstance(texts, tuple)
            or not 1 <= len(texts) <= 16
            or any(
                not isinstance(text, str) or not text.strip() or len(text) > 8192 for text in texts
            )
        ):
            raise GuideImportError("invalid_embedding_input")
        # CPU 特征提取移出事件循环；取消不强杀线程，单批字符上限限制剩余工作量。
        return await asyncio.to_thread(self._embed_batch, texts)

    def _embed_batch(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        result: list[tuple[float, ...]] = []
        for text in texts:
            clean = "".join(
                character
                for character in unicodedata.normalize("NFKC", text).lower()
                if character.isalnum()
            )
            if not clean:
                raise GuideImportError("invalid_embedding_input")
            values = [0.0] * self.profile.dimensions
            for width in (1, 2, 3):
                for index in range(len(clean) - width + 1):
                    digest = hashlib.sha256(clean[index : index + width].encode("utf-8")).digest()
                    bucket = int.from_bytes(digest[:4], "big") % len(values)
                    values[bucket] += width
            result.append(unit_vector(tuple(values), self.profile.dimensions))
        return tuple(result)
