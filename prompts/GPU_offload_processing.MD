Act as a Senior DevOps and LLM Infrastructure Engineer.

I want to offload heavy LLM inference from my local macOS machine (Apple Silicon M4) to a remote GPU instance on Vast.ai (e.g., RTX 4090 / A100), while keeping my current local Ollama setup active and running alongside it. 

The architecture must support a hybrid setup:
- Primary/Lightweight local tasks: Local Ollama (running on localhost:11434).
- Heavy/Coding/Agent tasks: Remote Vast.ai instance running vLLM (forwarded locally without port collisions, e.g., on localhost:8000).

Please provide a complete, production-ready, and automated guide including:

1. Remote Instance Setup (Vast.ai):
   - Recommended Docker image / template (CUDA + vLLM).
   - Shell launch script to start vLLM serving an OpenAI-compatible API with support for quantized models (AWQ/GPTQ/FP8) like Qwen/Qwen2.5-Coder-32B-Instruct or DeepSeek-Coder.
   - API key configuration (`--api-key`) to secure the remote endpoint.

2. Secure Networking & Non-Conflicting Port Forwarding:
   - A robust local macOS bash script using `autossh` (or standard `ssh -L`) to maintain a persistent, auto-reconnecting tunnel from remote port 8000 to `localhost:8000`.
   - Clear port mapping so local Ollama (11434) and remote vLLM (8000) coexist smoothly.

3. Client Integration & Hybrid Routing Examples:
   - Python snippet demonstrating fallback/routing between:
     * Provider A: Local Ollama (`http://localhost:11434/v1`)
     * Provider B: Remote Vast.ai vLLM (`http://localhost:8000/v1`)
   - LlamaIndex integration showing how to initialize both LLMs and route requests dynamically (e.g., lightweight extraction via local Ollama, complex agent/coding tasks via remote vLLM).
   - Neovim LLM plugin config (e.g., CodeCompanion / Avante / custom HTTP client) demonstrating dual-provider registration.

4. Operational & Cost Best Practices:
   - Handling Hugging Face cache persistence across spot/ephemeral instances on Vast.ai.
   - Health check command to verify both endpoints (`curl` checks for localhost:11434 and localhost:8000).
   - Clean teardown/reconnect scripts.

Ensure all shell scripts and Python code are modular, copy-paste ready, and include error handling.
