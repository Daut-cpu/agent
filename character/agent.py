from __future__ import annotations

import os
from typing import Any, Callable

import anthropic

from .memory import Memory
from .persona import Persona

DEFAULT_MODEL = os.environ.get("CHARACTER_MODEL", "claude-opus-5")
# Лёгкая беседа не требует глубоких рассуждений: low/medium дают быстрые ответы.
DEFAULT_EFFORT = os.environ.get("CHARACTER_EFFORT", "medium")

BETAS = [
    # Серверный fallback: если классификатор отклонит запрос, API сам
    # переиграет его на рекомендованной модели.
    "server-side-fallback-2026-07-01",
    # Серверная компакция: длинная история сжимается автоматически.
    "compact-2026-01-12",
]

REMEMBER_TOOL = {
    "name": "remember_fact",
    "description": (
        "Сохранить в долговременную память один важный факт о собеседнике "
        "(имя, увлечения, планы, предпочтения, важные события), чтобы помнить "
        "его в следующих разговорах. Формулируй коротко, в третьем лице: "
        "«Зовут Дима», «Учится на врача», «Боится высоты»."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "Один короткий факт о собеседнике."}
        },
        "required": ["fact"],
        "additionalProperties": False,
    },
    "eager_input_streaming": True,
}

MAX_STEPS = 8
REFUSAL_REPLY = "Давай лучше поговорим о чём-нибудь другом?"


class Character:
    """Виртуальный персонаж: ведёт диалог в образе и запоминает собеседника."""

    def __init__(
        self,
        persona: Persona,
        memory: Memory | None = None,
        client: Any | None = None,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
    ):
        self.persona = persona
        self.memory = memory or Memory()
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self._system_prompt = persona.system_prompt()

    def _system(self) -> list[dict[str, Any]]:
        # Личность неизменна — кэшируем её; факты меняются, поэтому идут после
        # точки кэширования и не сбрасывают кэш промпта.
        return [
            {"type": "text", "text": self._system_prompt, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": self.memory.facts_prompt()},
        ]

    def _stream_once(self, on_text: Callable[[str], None]) -> Any:
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=16000,
            system=self._system(),
            messages=self.memory.history,
            tools=[REMEMBER_TOOL],
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            context_management={"edits": [{"type": "compact_20260112"}]},
            fallbacks="default",
            betas=BETAS,
        ) as stream:
            for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    on_text(event.delta.text)
            return stream.get_final_message()

    def reply(self, user_text: str, on_text: Callable[[str], None] = lambda _: None) -> str:
        """Отправить реплику собеседника и получить ответ персонажа.

        `on_text` получает текст по мере генерации (для потокового вывода).
        """
        history = self.memory.history
        turn_start = len(history)
        history.append({"role": "user", "content": user_text})
        parts: list[str] = []

        def emit(chunk: str) -> None:
            parts.append(chunk)
            on_text(chunk)

        try:
            self._run_turn(emit)
        except BaseException:
            # Ошибка API или прерывание — откатываем незавершённый ход целиком.
            del history[turn_start:]
            raise
        if not history[turn_start:]:
            # Запрос отклонён: вежливо уводим разговор в сторону.
            prefix = "\n" if parts else ""
            on_text(prefix + REFUSAL_REPLY)
            return REFUSAL_REPLY
        self.memory.save()
        return "".join(parts)

    def _run_turn(self, emit: Callable[[str], None]) -> None:
        history = self.memory.history
        turn_start = len(history) - 1
        json_retries = 1
        for _ in range(MAX_STEPS):
            try:
                message = self._stream_once(emit)
            except ValueError:
                # Невалидный JSON во входе инструмента при потоковой передаче —
                # переспрашиваем модель один раз.
                if json_retries == 0:
                    raise
                json_retries -= 1
                continue

            if message.stop_reason == "refusal":
                # Откатываем ход, чтобы отклонённая реплика не осталась в истории.
                del history[turn_start:]
                return

            content = _echo_content(message.content)
            if message.stop_reason == "max_tokens":
                # Оборванный вызов инструмента не выполняем и не отправляем обратно.
                content = [b for b in content if b["type"] != "tool_use"] or [{"type": "text", "text": "…"}]
            history.append({"role": "assistant", "content": content})

            if message.stop_reason == "pause_turn":
                continue
            if message.stop_reason != "tool_use":
                break

            history.append({"role": "user", "content": self._run_tools(message.content)})

    def _run_tools(self, content: list[Any]) -> list[dict[str, Any]]:
        results = []
        for block in content:
            if block.type != "tool_use":
                continue
            fact = block.input.get("fact") if isinstance(block.input, dict) else None
            if block.name != REMEMBER_TOOL["name"]:
                result, is_error = f"Неизвестный инструмент: {block.name}", True
            elif not isinstance(fact, str) or not fact.strip():
                result, is_error = "INVALID_INPUT: поле fact должно быть непустой строкой", True
            elif self.memory.add_fact(fact):
                result, is_error = "Запомнено.", False
            else:
                result, is_error = "Это уже есть в памяти.", False
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": result, "is_error": is_error}
            )
        return results


def _echo_content(content: list[Any]) -> list[dict[str, Any]]:
    """Подготовить блоки ответа к отправке обратно в следующих запросах.

    Если во время ответа сработал fallback на другую модель, блоки thinking и
    tool_use до последней точки переключения отправлять нельзя.
    """
    blocks = [block.to_dict() for block in content]
    last_fallback = max((i for i, b in enumerate(blocks) if b["type"] == "fallback"), default=-1)
    dropped_before = {"thinking", "redacted_thinking", "tool_use", "fallback"}
    return [
        b for i, b in enumerate(blocks)
        if not (i <= last_fallback and b["type"] in dropped_before)
    ]
