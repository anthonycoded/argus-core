
"""Backward-compatible Global Chess configuration exports; no Core settings."""

from app.global_chess_config import FEEDS, KEYWORDS, global_chess_settings as settings

OLLAMA_BASE_URL = settings.ollama_base_url.get_secret_value()
OLLAMA_MODEL = settings.ollama_model
