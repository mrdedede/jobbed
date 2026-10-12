"""Gateway settings, read once at import from the environment (.env too)."""

import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    ollama_base_url: str = os.environ.get(
        "OLLAMA_BASE_URL", "http://ollama:11434")
    #: A local model grading a long posting can take well over a minute.
    ollama_timeout: float = float(os.environ.get("OLLAMA_TIMEOUT", "180"))


settings = Settings()
