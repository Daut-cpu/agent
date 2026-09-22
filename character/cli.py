from __future__ import annotations

import argparse
import sys
from pathlib import Path

import anthropic

from .agent import DEFAULT_EFFORT, DEFAULT_MODEL, Character
from .memory import Memory
from .persona import Persona

ROOT = Path(__file__).resolve().parent.parent

HELP = """Команды:
  /memory  — что персонаж помнит о тебе
  /reset   — начать разговор заново (факты сохранятся)
  /forget  — стереть всю память
  /exit    — выйти"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Чат с виртуальным персонажем")
    parser.add_argument("--persona", default=ROOT / "personas" / "asya.json", type=Path)
    parser.add_argument("--memory", type=Path, help="файл памяти (по умолчанию data/<persona>.json)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    args = parser.parse_args(argv)

    persona = Persona.load(args.persona)
    memory = Memory(args.memory or ROOT / "data" / f"{args.persona.stem}.json")
    character = Character(persona, memory, model=args.model, effort=args.effort)

    print(f"✨ {persona.name} — {persona.tagline}")
    print("(/help — список команд)\n")
    if not memory.history and persona.greeting:
        print(f"{persona.name}: {persona.greeting}\n")

    while True:
        try:
            user_text = input("Ты: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user_text:
            continue

        command = user_text.lower()
        if command in ("/exit", "/quit"):
            return 0
        if command == "/help":
            print(HELP + "\n")
            continue
        if command == "/memory":
            print("\n".join(f"• {f}" for f in memory.facts) or "Пока ничего.", end="\n\n")
            continue
        if command == "/reset":
            memory.reset_history()
            memory.save()
            print("Разговор начат заново.\n")
            continue
        if command == "/forget":
            memory.forget_all()
            memory.save()
            print("Память стёрта.\n")
            continue

        print(f"{persona.name}: ", end="", flush=True)
        try:
            text = character.reply(user_text, on_text=lambda t: print(t, end="", flush=True))
        except TypeError as e:
            if "authentication" not in str(e):
                raise
            print("\n[ошибка] Не найден ключ API — задай ANTHROPIC_API_KEY или выполни `ant auth login`.")
            return 1
        except anthropic.AuthenticationError:
            print("\n[ошибка] Нет доступа к API — задай ANTHROPIC_API_KEY или выполни `ant auth login`.")
            return 1
        except anthropic.RateLimitError:
            print("\n[ошибка] Превышен лимит запросов, попробуй чуть позже.\n")
            continue
        except anthropic.APIStatusError as e:
            print(f"\n[ошибка API {e.status_code}] {e.message}\n")
            continue
        except anthropic.APIConnectionError:
            print("\n[ошибка] Нет соединения с API.\n")
            continue
        if not text:
            print("…", end="")
        print("\n")


if __name__ == "__main__":
    sys.exit(main())
