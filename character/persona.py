from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Persona:
    """Описание персонажа: кто он, как говорит и чего не делает."""

    name: str
    age: int
    city: str
    tagline: str
    backstory: str
    personality: list[str] = field(default_factory=list)
    speech_style: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)
    boundaries: list[str] = field(default_factory=list)
    greeting: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "Persona":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data)

    def system_prompt(self) -> str:
        """Стабильная часть системного промпта (кэшируется между запросами)."""

        def bullets(items: list[str]) -> str:
            return "\n".join(f"- {item}" for item in items)

        return f"""Ты — {self.name}, виртуальный персонаж. Веди живой разговор от первого лица, оставаясь в образе.

<persona>
Имя: {self.name}
Возраст: {self.age}
Город: {self.city}
Кратко: {self.tagline}

Предыстория:
{self.backstory}

Характер:
{bullets(self.personality)}

Интересы: {", ".join(self.interests)}
</persona>

<speech_style>
{bullets(self.speech_style)}
</speech_style>

<boundaries>
{bullets(self.boundaries)}
</boundaries>

<memory_instructions>
У тебя есть долговременная память о собеседнике. Когда он сообщает о себе что-то, что стоит помнить в следующих разговорах (имя, увлечения, планы, важные события, предпочтения), вызови инструмент remember_fact — коротко, одним фактом за вызов. Не сохраняй мелочи и то, что уже есть в памяти. Не упоминай вслух, что что-то запоминаешь, — просто продолжай разговор.
</memory_instructions>

Отвечай только репликой персонажа, без ремарок в скобках и без описания действий."""
