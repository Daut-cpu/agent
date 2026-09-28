"""Командная строка: запуск сервера и управление пользователями."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from .security import Cipher
from .storage import Store

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = os.environ.get("ANALYST_DB", str(ROOT / "data" / "analyst.db"))


def _service(db: str, inline: bool = False):
    from .llm import ClaudeLLM
    from .service import AnalystService

    return AnalystService(Store(db), ClaudeLLM(), inline_jobs=inline)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m analyst", description="System Analyst Agent")
    parser.add_argument("--db", default=DEFAULT_DB, help="путь к SQLite (ANALYST_DB)")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="запустить API и веб-интерфейс")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    user = sub.add_parser("create-user", help="создать пользователя и выдать токен")
    user.add_argument("username")
    user.add_argument("--admin", action="store_true")

    token = sub.add_parser("rotate-token", help="выпустить новый токен пользователю")
    token.add_argument("username")

    sub.add_parser("gen-key", help="сгенерировать ключ шифрования ANALYST_ENCRYPTION_KEY")

    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("ANALYST_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "gen-key":
        print(Cipher.generate_key())
        return 0

    if args.command == "create-user":
        service = _service(args.db, inline=True)
        _, tok = service.create_user(args.username, args.admin)
        print(f"Пользователь {args.username} создан. Токен (показывается один раз):\n{tok}")
        return 0

    if args.command == "rotate-token":
        print(_service(args.db, inline=True).rotate_token(args.username))
        return 0

    import uvicorn

    from .api import create_app

    service = _service(args.db)
    if not service.store.count_users():
        _, tok = service.create_user("admin", is_admin=True)
        print(f"\nСоздан пользователь admin. Токен для входа (сохраните его):\n{tok}\n")
    if not service.store.cipher.enabled:
        logging.warning("ANALYST_ENCRYPTION_KEY не задан: документы хранятся без шифрования")
    uvicorn.run(create_app(service), host=args.host, port=args.port)
    return 0
