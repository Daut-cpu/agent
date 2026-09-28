"""Доступ к Claude: один метод — структурированный ответ по pydantic-схеме."""

from __future__ import annotations

import copy
import logging
import os
from typing import Any, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("ANALYST_MODEL", "claude-opus-5-5")
MAX_TOKENS = int(os.environ.get("ANALYST_MAX_TOKENS", "64000"))
BETAS = ["server-side-fallback-2026-07-01"]

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Модель не смогла вернуть корректный результат."""


class LLMRefusal(LLMError):
    """Запрос отклонён классификатором безопасности."""


class LLM(Protocol):
    model: str

    def structured(
        self, *, system: list[str], prompt: str, schema: type[T], effort: str = "medium"
    ) -> T: ...


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON-схема pydantic-модели в виде, который принимает structured outputs:
    у каждого объекта все поля обязательны и нет лишних свойств."""
    schema = copy.deepcopy(model.model_json_schema())

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema


class ClaudeLLM:
    """Структурированные ответы Claude.

    - `system` — список стабильных блоков (инструкция агента, контекст проекта);
      они кэшируются, поэтому повторные вызовы в рамках проекта дешевле.
    - Ответ ограничен JSON-схемой, поэтому парсинг не зависит от формулировок.
    - При отказе классификатора API сам повторяет запрос на резервной модели.
    """

    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL, max_tokens: int = MAX_TOKENS):
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens

    def structured(
        self, *, system: list[str], prompt: str, schema: type[T], effort: str = "medium"
    ) -> T:
        system_blocks = [{"type": "text", "text": text} for text in system if text]
        if system_blocks:
            system_blocks[-1]["cache_control"] = {"type": "ephemeral"}
        json_schema = strict_schema(schema)

        last_error: Exception | None = None
        for attempt in range(2):
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_blocks,
                messages=[{"role": "user", "content": prompt}],
                thinking={"type": "adaptive"},
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": json_schema},
                },
                fallbacks="default",
                betas=BETAS,
            ) as stream:
                message = stream.get_final_message()

            if message.stop_reason == "refusal":
                raise LLMRefusal("Модель отклонила запрос. Переформулируйте задачу.")
            if message.stop_reason == "max_tokens":
                raise LLMError("Ответ модели оборван по лимиту токенов. Сузьте задачу.")
            text = "".join(b.text for b in message.content if b.type == "text")
            try:
                return schema.model_validate_json(text)
            except ValidationError as e:
                log.warning("Невалидный ответ %s (попытка %d): %s", schema.__name__, attempt + 1, e)
                last_error = e
        raise LLMError(f"Модель вернула некорректную структуру {schema.__name__}") from last_error
