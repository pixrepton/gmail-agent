"""OpenAI-compatible embeddings helper for bounded mailbox-memory enrichment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from config import Settings


@dataclass(slots=True)
class EmbeddingRuntime:
    base_url: str
    api_key: str
    model: str
    dimensions: int = 0
    timeout: int = 60

    @property
    def endpoint(self) -> str:
        base = str(self.base_url or "").strip().rstrip("/")
        if not base:
            return ""
        if base.endswith("/v1"):
            return f"{base}/embeddings"
        return f"{base}/v1/embeddings"

    def embed_texts(self, texts: list[str]) -> list[list[float] | None]:
        if not texts:
            return []
        if not self.endpoint or not self.model:
            raise RuntimeError("Embeddings endpoint/model is not configured.")

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload: dict[str, Any] = {
            "model": self.model,
            "input": texts,
        }
        if self.dimensions > 0:
            payload["dimensions"] = self.dimensions

        response = requests.post(
            self.endpoint,
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            detail = ""
            try:
                err_body = response.json()
                detail = str(err_body.get("error") or err_body)[:500]
            except ValueError:
                detail = (response.text or "")[:500]
            raise RuntimeError(
                f"Embedding request failed with status {response.status_code}."
                + (f" {detail}" if detail else "")
            )

        try:
            body = response.json()
        except ValueError as exc:  # pragma: no cover - defensive
            raise RuntimeError("Embedding response was not valid JSON.") from exc

        rows = body.get("data")
        if not isinstance(rows, list):
            raise RuntimeError("Embedding response missing `data` list.")

        vectors: list[list[float] | None] = []
        for item in rows:
            if not isinstance(item, dict):
                vectors.append(None)
                continue
            raw_embedding = item.get("embedding")
            if not isinstance(raw_embedding, list):
                vectors.append(None)
                continue
            try:
                vectors.append([float(value) for value in raw_embedding])
            except (TypeError, ValueError):
                vectors.append(None)
        return vectors


def build_embedding_runtime(settings: Settings) -> EmbeddingRuntime | None:
    if not bool(getattr(settings, "mailbox_memory_vector_enabled", False)):
        return None
    base_url = str(getattr(settings, "openai_compat_embedding_base_url", "") or "").strip()
    if not base_url:
        base_url = str(getattr(settings, "openai_compat_base_url", "") or "").strip()
    model = str(getattr(settings, "openai_compat_embedding_model", "") or "").strip()
    if not base_url or not model:
        return None
    api_key = str(getattr(settings, "openai_compat_embedding_api_key", "") or "").strip()
    return EmbeddingRuntime(
        base_url=base_url,
        api_key=api_key,
        model=model,
        dimensions=int(getattr(settings, "openai_compat_embedding_dimensions", 0) or 0),
        timeout=int(getattr(settings, "http_timeout", 60) or 60),
    )


__all__ = [
    "EmbeddingRuntime",
    "build_embedding_runtime",
]
