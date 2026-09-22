from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_FACTS = 200


class Memory:
    """Долговременная память персонажа: факты о собеседнике и история диалога.

    Хранится в одном JSON-файле, поэтому разговор можно продолжить после
    перезапуска. История хранит полные блоки ответа модели (включая
    thinking и tool_use) — их нужно отправлять обратно без изменений.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.facts: list[str] = []
        self.history: list[dict[str, Any]] = []
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.facts = list(data.get("facts", []))
            self.history = list(data.get("history", []))

    def add_fact(self, fact: str) -> bool:
        fact = " ".join(fact.split())
        if not fact or fact.casefold() in (f.casefold() for f in self.facts):
            return False
        self.facts.append(fact)
        del self.facts[:-MAX_FACTS]
        return True

    def facts_prompt(self) -> str:
        if not self.facts:
            return "<user_facts>Пока ничего не известно — это первое знакомство.</user_facts>"
        lines = "\n".join(f"- {fact}" for fact in self.facts)
        return f"<user_facts>\nЧто ты помнишь о собеседнике:\n{lines}\n</user_facts>"

    def reset_history(self) -> None:
        self.history.clear()

    def forget_all(self) -> None:
        self.facts.clear()
        self.history.clear()

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"facts": self.facts, "history": self.history}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)
