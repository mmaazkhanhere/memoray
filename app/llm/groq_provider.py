import json
import logging
from time import perf_counter
from typing import Any

from app.config import Settings
from app.llm.protocol import LLMProviderError, LLMResponse, ToolCall

logger = logging.getLogger(__name__)


class GroqProvider:
    """Groq adapter; the agent depends only on the LLMProvider protocol."""

    def __init__(self, settings: Settings):
        if not settings.groq_api_key:
            raise ValueError("GROQ_API_KEY is required to use the Groq provider")
        from groq import Groq

        self.client = Groq(api_key=settings.groq_api_key)
        self.model = settings.groq_model

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        started_at = perf_counter()
        logger.info("Calling Groq model=%s tools=%s", self.model, [tool["function"]["name"] for tool in tools])
        try:
            request = {"model": self.model, "messages": messages}
            if tools:
                request["tools"] = tools
                request["tool_choice"] = "auto"
            else:
                request["tool_choice"] = "none"
            response = self.client.chat.completions.create(**request)
        except Exception as exc:
            logger.exception("Groq call failed model=%s", self.model)
            raise LLMProviderError("The language model provider rejected the request") from exc
        message = response.choices[0].message
        calls = [
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=json.loads(call.function.arguments or "{}"),
            )
            for call in (message.tool_calls or [])
        ]
        logger.info(
            "Groq response received model=%s elapsed_ms=%.1f tool_calls=%s has_text=%s",
            self.model,
            (perf_counter() - started_at) * 1000,
            [call.name for call in calls],
            bool(message.content),
        )
        return LLMResponse(text=message.content, tool_calls=calls)

