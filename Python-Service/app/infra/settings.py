from pydantic import BaseModel
import os

from infra.settings_store import settings_store
from infra.log_paths import configured_log_dir


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
    # Cohere credentials are UI-managed through settings.db; this empty field
    # lets the dynamic overlay provide its normal write-only value.
    cohere_api_key: str = ""
    # Cloudflare Workers AI credentials are UI-managed through settings.db.
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    cloudflare_embedding_model: str = "@cf/baai/bge-base-en-v1.5"
    ollama_llm_model: str = os.getenv("OLLAMA_LLM_MODEL", "gemma3-4b")
    # How long the local Ollama chat model may remain resident after its last
    # request. Supports Ollama duration strings such as ``30s`` or ``5m``;
    # ``0`` unloads immediately and ``-1`` disables application-side unloads.
    ollama_keep_alive: str = os.getenv("OLLAMA_KEEP_ALIVE", "5m")
    # Optional OpenAI-compatible embedding server (LM Studio / Bionic). When set,
    # embeddings are produced via `{base}/v1/embeddings`; when empty they stay on
    # the Ollama server (`ollama_base_url` /api/embeddings).
    embedding_base_url: str = os.getenv("EMBEDDING_BASE_URL", "")
    # Optional override for the LOCAL chat provider (e.g. LM Studio/Bionic).
    # Defaults to the Ollama server when unset. Embeddings/classification stay
    # on the Ollama server regardless.
    local_llm_url: str = os.getenv("LOCAL_LLM_URL", "")
    local_llm_model: str = os.getenv("LOCAL_LLM_MODEL", "")
    ollama_eval_model: str = os.getenv("OLLAMA_EVAL_MODEL", "")
    llm_remote_base_url: str = os.getenv("LLM_REMOTE_BASE_URL", "")
    llm_remote_model: str = os.getenv("LLM_REMOTE_MODEL", "")
    llm_remote_api_key: str = os.getenv("LLM_REMOTE_API_KEY", "")
    # Kill switch for model tool calls in chat (agent_tools/runtime.py).
    agent_tools_enabled: bool = os.getenv("AGENT_TOOLS_ENABLED", "true").strip().lower() not in {"0", "false", "no"}

    # Documentation retrieval (Context7 primary, GitMCP fallback). The doc
    # lookup key is read straight from CONTEXT7_API_KEY when present — never
    # stored through the settings UI (no context7ApiKey dynamic key).
    doc_retrieval_enabled: bool = os.getenv("DOC_RETRIEVAL_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    doc_context7_enabled: bool = os.getenv("DOC_CONTEXT7_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    doc_gitmcp_enabled: bool = os.getenv("DOC_GITMCP_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    doc_strict_mode: bool = os.getenv("DOC_STRICT_MODE", "false").strip().lower() in ("1", "true", "yes", "on")
    doc_context7_timeout: float = float(os.getenv("DOC_CONTEXT7_TIMEOUT", "10"))
    doc_context7_max_retries: int = int(os.getenv("DOC_CONTEXT7_MAX_RETRIES", "1"))
    doc_gitmcp_timeout: float = float(os.getenv("DOC_GITMCP_TIMEOUT", "15"))
    context7_api_key_env: str = os.getenv("CONTEXT7_API_KEY_ENV", "CONTEXT7_API_KEY")

    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.7"))
    top_p: float = float(os.getenv("LLM_TOP_P", "0.9"))
    llm_context_window_tokens: int = int(os.getenv("LLM_CONTEXT_WINDOW_TOKENS", "0"))
    assessment_request_token_limit: int = int(os.getenv("ASSESSMENT_REQUEST_TOKEN_LIMIT", "0"))

    # Context-window preview toggle: "off" | "inline" | "gate".
    context_preview_mode: str = os.getenv("CONTEXT_PREVIEW_MODE", "off")

    # Auto-index extracted domain concepts (concept store + Qdrant mirror) after
    # a save, and enrich /answer prompts with recognized domain concepts.
    domain_concepts_enabled: bool = os.getenv(
        "DOMAIN_CONCEPTS_ENABLED", "true"
    ).strip().lower() in ("1", "true", "yes", "on")

    reranker_enabled: bool = os.getenv(
        "RERANKER_ENABLED", "false"
    ).strip().lower() in ("1", "true", "yes", "on")
    reranker_model: str = os.getenv(
        "RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"
    )
    reranker_max_candidates: int = int(os.getenv("RERANKER_MAX_CANDIDATES", "40"))
    reranker_timeout: float = float(os.getenv("RERANKER_TIMEOUT", "10"))

    log_deletion_mode: str = os.getenv("LOG_DELETION_MODE", "soft").strip().lower()

    minio_endpoint: str = os.getenv("AWS_ENDPOINT", "minio:9000")
    minio_access_key: str = os.getenv("AWS_ACCESS_KEY_ID", "minio")
    minio_secret_key: str = os.getenv("AWS_SECRET_ACCESS_KEY", "minio123")
    minio_tus_bucket: str = os.getenv("AWS_TUS_BUCKET", "tus-uploads")
    upload_token_secret: str = os.getenv("UPLOAD_TOKEN_SECRET", "change-me")
    tus_public_url: str = os.getenv("TUS_PUBLIC_URL", "http://localhost:1080/files/")
    workspace_root: str = os.getenv("WORKSPACE_ROOT", "/app/workspaces")
    php_cli_root: str = os.getenv("PHP_CLI_ROOT", "/opt/php-cli")


    lsp_timeout: float = float(os.getenv("LSP_TIMEOUT", "10"))
    lsp_max_retries: int = int(os.getenv("LSP_MAX_RETRIES", "2"))
    lsp_warmup_enabled: bool = os.getenv(
        "LSP_WARMUP_ENABLED", "false"
    ).strip().lower() in ("1", "true", "yes", "on")
    lsp_validation_enabled: bool = os.getenv(
        "LSP_VALIDATION_ENABLED", "true"
    ).strip().lower() in ("1", "true", "yes", "on")

    enable_typo_correction: bool = os.getenv(
        "ENABLE_TYPO_CORRECTION", "false"
    ).strip().lower() in ("1", "true", "yes", "on")

    # LLM-assisted framework detection: one-shot fallback probe run only when
    # no static plugin detects the project confidently (env-only flag — the
    # probe never keeps state; context is freed when the call returns).
    llm_detect_enabled: bool = os.getenv(
        "LLM_DETECT_ENABLED", "true"
    ).strip().lower() in ("1", "true", "yes", "on")

    download_dir: str = os.getenv("DOWNLOAD_DIR", "/app/downloads")
    download_token_secret: str = os.getenv("DOWNLOAD_TOKEN_SECRET", "change-me")
    internal_secret: str = os.getenv("RAG_CALLBACK_SECRET", "change-me")
    log_dir: str = configured_log_dir()


class RuntimeSettings:
    """Dynamic settings overlay.

    UI-editable values are read from the SQLite store at access time; everything
    else falls back to the environment-derived defaults. Consumers keep using
    `settings.<attr>` unchanged, and updated values take effect immediately.
    """

    _DYNAMIC = {
        "cohere_api_key": "cohereApiKey",
        "cloudflare_account_id": "cloudflareAccountId",
        "cloudflare_api_token": "cloudflareApiToken",
        "cloudflare_embedding_model": "cloudflareEmbeddingModel",
        "ollama_base_url": "ollamaBaseUrl",
        "ollama_embedding_model": "ollamaEmbedModel",
        "embedding_base_url": "embeddingBaseUrl",
        "ollama_llm_model": "ollamaLlmModel",
        "ollama_eval_model": "ollamaEvalModel",
        "local_llm_url": "localLlmUrl",
        "local_llm_model": "localLlmModel",
        "llm_remote_base_url": "llmRemoteBaseUrl",
        "llm_remote_model": "llmRemoteModel",
        "llm_remote_api_key": "llmRemoteApiKey",
        "doc_retrieval_enabled": "docRetrievalEnabled",
        "doc_context7_enabled": "docContext7Enabled",
        "doc_gitmcp_enabled": "docGitmcpEnabled",
        "doc_strict_mode": "docStrictMode",
        "doc_context7_timeout": "docContext7TimeoutS",
        "doc_gitmcp_timeout": "docGitmcpTimeoutS",
        "qdrant_url": "qdrantUrl",
        "qdrant_api_key": "qdrantApiKey",
        "temperature": "temperature",
        "top_p": "topP",
        "context_preview_mode": "contextPreviewMode",
        "domain_concepts_enabled": "domainConceptsEnabled",
        "reranker_enabled": "rerankerEnabled",
        "reranker_model": "rerankerModel",
        "reranker_max_candidates": "rerankerMaxCandidates",
        "reranker_timeout": "rerankerTimeoutS",
        "log_deletion_mode": "logDeletionMode",
    }

    def __init__(self) -> None:
        self._env = Settings()

    def __getattr__(self, name: str):
        store_key = self._DYNAMIC.get(name)
        if store_key:
            value = settings_store.get(store_key)
            if value not in (None, ""):
                return value
        if name == "embedding_provider":
            configured = settings_store.get("embeddingProvider")
            if configured:
                return configured
            # Preserve pre-provider UI config which selected OpenAI-compatible
            # embeddings by setting a base URL.
            return "openAICompatible" if self.embedding_base_url else "ollama"
        if name == "ollama_eval_model":
            return self.ollama_llm_model
        value = getattr(self._env, name)
        if name == "log_deletion_mode" and value not in {"soft", "hard"}:
            return "soft"
        return value

    def revision(self) -> int:
        return settings_store.revision


settings = RuntimeSettings()
