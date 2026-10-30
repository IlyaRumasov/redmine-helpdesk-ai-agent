"""Локальні налаштування інструментів: адреси, id кредів і таблиць n8n.

Значення читаються з tools/local_settings.json (не комітиться). Шаблон: tools/local_settings.example.json.
"""
import json
from pathlib import Path

S = json.loads((Path(__file__).resolve().parent / "local_settings.json").read_text(encoding="utf-8"))


def cred(key, kind):
    """Посилання на кред n8n у форматі вузла: {kind: {id, name}}."""
    return {kind: dict(S[key])}
