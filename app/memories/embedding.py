from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol
from urllib import error, request

if TYPE_CHECKING:
    from app.config import Settings


class EmbeddingError(RuntimeError):
    """A controlled error from the configured embedding provider."""

    def __init__(self, code: str, message: str = "embedding provider failed"):
        super().__init__(message)
        self.code = code


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int

    def embed(self, text: str) -> Sequence[float]:
        ...


class OpenAICompatibleEmbeddingProvider:
    """Small dependency-free adapter for OpenAI-compatible /embeddings endpoints."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimensions: int,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 20.0,
    ):
        if not api_key:
            raise ValueError("EMBEDDING_API_KEY is required to use memory retrieval")
        if dimensions < 1:
            raise ValueError("EMBEDDING_DIMENSIONS must be positive")
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def embed(self, text: str) -> Sequence[float]:
        payload = json.dumps({"model": self.model, "input": text}).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            raise EmbeddingError("provider_http_error") from exc
        except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise EmbeddingError("provider_unavailable") from exc

        try:
            values = body["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise EmbeddingError("invalid_provider_response") from exc
        if not isinstance(values, list) or len(values) != self.dimensions:
            raise EmbeddingError("dimension_mismatch")
        try:
            return [float(value) for value in values]
        except (TypeError, ValueError) as exc:
            raise EmbeddingError("invalid_provider_response") from exc


class GeminiEmbeddingProvider:
    """Gemini Developer API adapter behind the provider-neutral embedding interface."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-embedding-001",
        dimensions: int = 1536,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_seconds: float = 20.0,
    ):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required to use memory retrieval")
        if dimensions < 1:
            raise ValueError("EMBEDDING_DIMENSIONS must be positive")
        self.api_key = api_key
        self.model = model.removeprefix("models/")
        self.dimensions = dimensions
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def embed(self, text: str) -> Sequence[float]:
        return self._embed(text, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> Sequence[float]:
        return self._embed(text, "RETRIEVAL_QUERY")

    def _embed(self, text: str, task_type: str) -> Sequence[float]:
        payload: dict[str, object] = {
            "content": {"parts": [{"text": text}]},
            "taskType": task_type,
        }
        if self.dimensions != 3072:
            payload["outputDimensionality"] = self.dimensions
        req = request.Request(
            f"{self.base_url}/models/{self.model}:embedContent",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            raise EmbeddingError("provider_http_error") from exc
        except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise EmbeddingError("provider_unavailable") from exc

        try:
            values = body["embedding"]["values"]
        except (KeyError, IndexError, TypeError) as exc:
            raise EmbeddingError("invalid_provider_response") from exc
        if not isinstance(values, list) or len(values) != self.dimensions:
            raise EmbeddingError("dimension_mismatch")
        try:
            numeric_values = [float(value) for value in values]
        except (TypeError, ValueError) as exc:
            raise EmbeddingError("invalid_provider_response") from exc
        # gemini-embedding-001 requires manual normalization for reduced dimensions.
        norm = math.sqrt(sum(value * value for value in numeric_values))
        if norm == 0:
            raise EmbeddingError("zero_vector")
        return [value / norm for value in numeric_values]


def create_embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    """Construct the configured adapter without coupling memory logic to a vendor."""
    api_key = settings.embedding_api_key or settings.gemini_api_key
    if not api_key:
        return None
    provider_name = settings.embedding_provider.casefold().strip()
    if provider_name == "gemini":
        return GeminiEmbeddingProvider(
            api_key=api_key,
            base_url=settings.embedding_base_url,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
    if provider_name in {"openai", "openai-compatible"}:
        return OpenAICompatibleEmbeddingProvider(
            api_key=api_key,
            base_url=settings.embedding_base_url,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
    raise ValueError(f"unsupported embedding provider: {settings.embedding_provider}")
