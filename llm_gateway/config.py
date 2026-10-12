# llm_gateway/config.py
import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    ollama_base_url: str = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")

settings = Settings()
