<?php

namespace App\Services\CodeParser\Embedding;

/** block exists               │
 * │ docblock        │ string|null   │ Raw docblock text (included in embed)    │
 * │ code            │ string        │ Exact source text (NOT embedded raw —    │
 * │                 │               │ use toEmbeddableText() for that)         │
 * │ indexed_at      │ string (ISO)  │ When this chunk was last indexed         │
 * │ content_hash    │ string        │ sha256 of code — skip re-index if same  │
 * └─────────────────┴───────────────┴──────────────────────────────────────────┘
 *
 * RETRIEVAL STRATEGIES enabled by this schema:
 *
 *  1. Pure semantic search
 *     → embed the query, search by vector similarity
 *     → good for: "how is payment retried?"
 *
 *  2. Filtered semantic search
 *     → semantic search WHERE file = 'src/Payment/...' AND type = 'method'
 *     → good for: scoping search to a module
 *
 *  3. Symbol lookup
 *     → exact match on name or class fields (no embedding needed)
 *     → good for: "give me the definition of processRefund()"
 *
 *  4. Call graph expansion
 *     → after finding chunk X, fetch all chunks WHERE name IN X.calls
 *     → good for: building richer exception context automatically
 *
 *  5. Hybrid (BM25 + vector)
 *     → Qdrant supports sparse+dense vectors natively
 *     → good for: combining keyword match on function names with semantic search
 */
final readonly class LocalEmbedder
{
    public function __construct(
        private string $ollamaUrl = 'http://localhost:11434',
        private string $model = 'nomic-embed-text',  // or mxbai-embed-large
    )
    {
    }

    /** Returns a float[] embedding vector */
    public function embed(string $text): array
    {
        $response = $this->post('/api/embeddings', [
            'model' => $this->model,
            'prompt' => $text,
        ]);

        return $response['embedding'] ?? throw new \RuntimeException('No embedding returned');
    }

    /** Batch embed — reduces round-trips */
    public function embedBatch(array $texts): array
    {
        return array_map(fn($t) => $this->embed($t), $texts);
    }

    private function post(string $path, array $body): array
    {
        $ch = curl_init($this->ollamaUrl . $path);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => json_encode($body),
            CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
            CURLOPT_TIMEOUT => 60,
        ]);
        $result = curl_exec($ch);
        curl_close($ch);
        return json_decode($result, true);
    }
}
