"""Ollama routine adapter — embedding-based procedural matching.

Uses Ollama's ``/api/embed`` endpoint to compute embeddings for task
descriptions and procedure blocks, then matches via cosine similarity.
Falls back to :class:`SimpleRoutineAdapter` if Ollama is unreachable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import urllib.error
import urllib.request
from typing import Any

from cortiva.adapters.protocols import FamiliaritySignal, MemoryRecord
from cortiva.adapters.routine.simple import SimpleRoutineAdapter

logger = logging.getLogger("cortiva.routine.ollama")


# ---------------------------------------------------------------------------
# Embedding client (stdlib only — no httpx/aiohttp required)
# ---------------------------------------------------------------------------


class EmbeddingClient:
    """Talks to Ollama's embedding endpoint using only stdlib."""

    def __init__(
        self,
        model: str = "nomic-embed-text",
        endpoint: str = "http://localhost:11434",
        timeout: float = 30.0,
    ) -> None:
        self._model = model
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout

    async def embed(self, text: str) -> list[float]:
        """Get embedding vector for a single text string."""
        return await asyncio.to_thread(self._embed_sync, text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts, one at a time."""
        results = []
        for text in texts:
            results.append(await self.embed(text))
        return results

    async def is_available(self) -> bool:
        """Check if Ollama is reachable."""
        try:
            return await asyncio.to_thread(self._ping_sync)
        except Exception:
            return False

    def _embed_sync(self, text: str) -> list[float]:
        url = f"{self._endpoint}/api/embed"
        payload = json.dumps({"model": self._model, "input": text}).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read())
        embeddings = data.get("embeddings")
        if embeddings and len(embeddings) > 0:
            return embeddings[0]
        raise ValueError(f"Unexpected Ollama response: {data}")

    def _ping_sync(self) -> bool:
        req = urllib.request.Request(f"{self._endpoint}/api/tags")
        with urllib.request.urlopen(req, timeout=5.0):
            return True


# ---------------------------------------------------------------------------
# Procedure index
# ---------------------------------------------------------------------------


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class ProcedureIndex:
    """Embeds and indexes an agent's procedures.md for fast similarity search."""

    def __init__(self, client: EmbeddingClient) -> None:
        self._client = client
        self._blocks: list[str] = []
        self._embeddings: list[list[float]] = []

    async def build(self, procedures_text: str) -> None:
        """Parse procedures into blocks and embed each one."""
        self._blocks = self._split_blocks(procedures_text)
        if not self._blocks:
            self._embeddings = []
            return
        self._embeddings = await self._client.embed_batch(self._blocks)

    async def search(self, query: str, top_k: int = 3) -> list[tuple[str, float]]:
        """Return top-k (block_text, similarity) pairs."""
        if not self._embeddings:
            return []
        query_vec = await self._client.embed(query)
        scored = [
            (block, _cosine_similarity(query_vec, emb))
            for block, emb in zip(self._blocks, self._embeddings)
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _split_blocks(text: str) -> list[str]:
        blocks: list[str] = []
        current: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") and current:
                block = "\n".join(current).strip()
                if block:
                    blocks.append(block)
                current = [stripped]
            else:
                current.append(stripped)
        if current:
            block = "\n".join(current).strip()
            if block:
                blocks.append(block)
        return blocks


# ---------------------------------------------------------------------------
# Ollama routine adapter
# ---------------------------------------------------------------------------


class OllamaRoutineAdapter:
    """Routine cognition via Ollama embeddings.

    Embeds the task description and compares against procedure blocks
    using cosine similarity.  If Ollama is unavailable, delegates to
    :class:`SimpleRoutineAdapter` as a fallback.
    """

    def __init__(
        self,
        *,
        model: str = "nomic-embed-text",
        endpoint: str = "http://localhost:11434",
        confidence_threshold: float = 0.70,
        defer_threshold: float = 0.45,
        timeout: float = 30.0,
    ) -> None:
        self._client = EmbeddingClient(model=model, endpoint=endpoint, timeout=timeout)
        self._confidence_threshold = confidence_threshold
        self._defer_threshold = defer_threshold
        self._fallback = SimpleRoutineAdapter(
            confidence_threshold=0.30, defer_threshold=0.15
        )
        self._index_cache: dict[str, ProcedureIndex] = {}

    async def assess(
        self,
        agent_id: str,
        task_description: str,
        procedural_index: str,
        familiarity: FamiliaritySignal,
    ) -> dict[str, Any]:
        if not await self._client.is_available():
            logger.debug("Ollama unavailable, falling back to simple adapter")
            return await self._fallback.assess(
                agent_id, task_description, procedural_index, familiarity
            )

        # Build or reuse procedure index for this agent
        index = self._index_cache.get(agent_id)
        if index is None:
            index = ProcedureIndex(self._client)
            self._index_cache[agent_id] = index

        try:
            await index.build(procedural_index)
        except Exception:
            logger.warning("Failed to build procedure index, falling back", exc_info=True)
            return await self._fallback.assess(
                agent_id, task_description, procedural_index, familiarity
            )

        try:
            results = await index.search(task_description, top_k=1)
        except Exception:
            logger.warning("Embedding search failed, falling back", exc_info=True)
            return await self._fallback.assess(
                agent_id, task_description, procedural_index, familiarity
            )

        if not results:
            return {
                "action": "escalate",
                "procedure_match": None,
                "confidence": 0.0,
                "context_for_conscious": None,
            }

        best_block, best_score = results[0]

        # Familiarity boost
        if familiarity.strength == "routine":
            best_score = min(1.0, best_score + 0.05)
        elif familiarity.strength == "familiar":
            best_score = min(1.0, best_score + 0.02)

        if best_score >= self._confidence_threshold:
            return {
                "action": "procedural",
                "procedure_match": best_block,
                "confidence": round(best_score, 4),
                "context_for_conscious": None,
                "result": f"Matched procedure (confidence {best_score:.0%}): {best_block[:200]}",
            }

        if best_score < self._defer_threshold:
            return {
                "action": "escalate",
                "procedure_match": None,
                "confidence": round(best_score, 4),
                "context_for_conscious": None,
            }

        return {
            "action": "defer",
            "procedure_match": best_block,
            "confidence": round(best_score, 4),
            "context_for_conscious": None,
        }

    async def compile_context(
        self,
        agent_id: str,
        identity: str,
        memories: list[MemoryRecord],
        familiarity: FamiliaritySignal,
        task: str,
        additional: dict[str, str] | None = None,
    ) -> str:
        return await self._fallback.compile_context(
            agent_id, identity, memories, familiarity, task, additional
        )
