from pydantic import BaseModel
import os


class Settings(BaseModel):
    app_name: str = "code-intelligence-service"
    report_path: str = os.getenv("REPORT_PATH", "/app/report.json")

    qdrant_url: str = os.getenv("QDRANT_URL", "http://qdrant:6333")
    qdrant_collection: str = os.getenv("QDRANT_COLLECTION", "code_symbols")

    embedding_backend: str = os.getenv("EMBEDDING_BACKEND", "ollama")
    embedding_model: str = os.getenv(
        "EMBEDDING_MODEL",
        "sentence-transformers/all-MiniLM-L6-v2",
    )

    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
    ollama_embedding_model: str = os.getenv(
        "OLLAMA_EMBED_MODEL",
        "nomic-embed-text",
    )


settings = Settings()

