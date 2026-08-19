from pydantic import BaseModel
import os

from settings_store import settings_store


class Settings(BaseModel):
    app_name: str = "code-intelligence-service"
    report_path: str = os.getenv("REPORT_PATH", "/app/report.json")

    qdrant_url: str = os.getenv("QDRANT_URL", "http://qdrant:6333")
    qdrant_collection: str = os.getenv("QDRANT_COLLECTION", "code_symbols")
    qdrant_api_key: str = os.getenv("QDRANT_API_KEY", "")

    neo4j_url: str = os.getenv("NEO4J_URL", "bolt://neo4j:7687")
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "password")
    neo4j_database: str = os.getenv("NEO4J_DATABASE", "neo4j")

    embedding_backend: str = os.getenv("EMBEDDING_BACKEND", "ollama")
    embedding_model: str = os.getenv(
        "EMBEDDING_MODEL",
        "sentence-transformers/all-MiniLM-L6-v2",
    )

    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    ollama_embedding_model: str = os.getenv("OLLAMA_EMBED_MODEL", "BGE-M3")
    ollama_llm_model: str = os.getenv("OLLAMA_LLM_MODEL", "gemma3-4b")

    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.7"))
    top_p: float = float(os.getenv("LLM_TOP_P", "0.9"))
    chunk_size: int = int(os.getenv("CHUNK_SIZE", "500"))
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", "50"))

    enable_typo_correction: bool = os.getenv(
        "ENABLE_TYPO_CORRECTION", "false"
    ).strip().lower() in ("1", "true", "yes", "on")

    download_dir: str = os.getenv("DOWNLOAD_DIR", "/app/downloads")
    download_token_secret: str = os.getenv("DOWNLOAD_TOKEN_SECRET", "change-me")
    internal_secret: str = os.getenv("RAG_CALLBACK_SECRET", "change-me")
    log_dir: str = os.getenv("LOG_DIR", "/app/logs")


class RuntimeSettings:
    """Dynamic settings overlay.

    UI-editable values are read from the SQLite store at access time; everything
    else falls back to the environment-derived defaults. Consumers keep using
    `settings.<attr>` unchanged, and updated values take effect immediately.
    """

    _DYNAMIC = {
        "ollama_base_url": "ollamaBaseUrl",
        "ollama_embedding_model": "ollamaEmbedModel",
        "ollama_llm_model": "ollamaLlmModel",
        "qdrant_url": "qdrantUrl",
        "qdrant_api_key": "qdrantApiKey",
        "temperature": "temperature",
        "top_p": "topP",
        "chunk_size": "chunkSize",
        "chunk_overlap": "chunkOverlap",
    }

    def __init__(self) -> None:
        self._env = Settings()

    def __getattr__(self, name: str):
        store_key = self._DYNAMIC.get(name)
        if store_key:
            value = settings_store.get(store_key)
            if value not in (None, ""):
                return value
        return getattr(self._env, name)

    def revision(self) -> int:
        return settings_store.revision


settings = RuntimeSettings()
