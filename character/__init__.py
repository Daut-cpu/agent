"""Виртуальный персонаж на базе Claude API."""

from .persona import Persona
from .memory import Memory
from .agent import Character

__all__ = ["Persona", "Memory", "Character"]
