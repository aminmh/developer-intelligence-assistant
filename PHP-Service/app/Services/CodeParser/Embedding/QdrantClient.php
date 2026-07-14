<?php

namespace App\Services\CodeParser\Embedding;

final readonly class QdrantClient
{
    public function __construct(
        private string $url = 'http://localhost:6333',
        private string $collection = 'code_chunks',
        private int    $vectorSize = 768,   // nomic-embed-text = 768; mxbai-embed-large = 1024
    )
    {
    }

    /** Create the collection if it doesn't exist yet */
    public function ensureCollection(): void
    {
        // Check if collection exists
        $status = $this->request('GET', "/collections/{$this->collection}");
        if (isset($status['result'])) return;

        // Create with named vectors + payload index for filtered search
        $this->request('PUT', "/collections/{$this->collection}", [
            'vectors' => [
                'size' => $this->vectorSize,
                'distance' => 'Cosine',
            ],
        ]);

        // Index the fields we filter on most often
        foreach (['type', 'name', 'class', 'file', 'modifiers', 'calls'] as $field) {
            $this->request('PUT', "/collections/{$this->collection}/index", [
                'field_name' => $field,
                'field_schema' => in_array($field, ['modifiers', 'calls']) ? 'keyword[]' : 'keyword',
            ]);
        }

        echo "Collection '{$this->collection}' created.\n";
    }

    /** Upsert a batch of points */
    public function upsertPoints(array $points): void
    {
        $this->request('PUT', "/collections/{$this->collection}/points", [
            'points' => $points,
        ]);
    }

    /**
     * Semantic search — returns top-k chunks
     *
     * @param float[] $queryVector
     * @param array $filter Qdrant filter DSL (optional)
     */
    public function search(array $queryVector, int $topK = 5, array $filter = []): array
    {
        $body = [
            'vector' => $queryVector,
            'limit' => $topK,
            'with_payload' => true,
        ];
        if ($filter) $body['filter'] = $filter;

        $result = $this->request('POST', "/collections/{$this->collection}/points/search", $body);
        return $result['result'] ?? [];
    }

    /** Exact symbol lookup by function/method name */
    public function lookupByName(string $name, ?string $class = null): array
    {
        $must = [['key' => 'name', 'match' => ['value' => $name]]];
        if ($class) {
            $must[] = ['key' => 'class', 'match' => ['value' => $class]];
        }
        $result = $this->request('POST', "/collections/{$this->collection}/points/scroll", [
            'filter' => ['must' => $must],
            'with_payload' => true,
            'limit' => 10,
        ]);
        return $result['result']['points'] ?? [];
    }

    /** Call graph expansion: fetch all chunks that match the names called by a chunk */
    public function fetchCallees(array $callNames): array
    {
        if (!$callNames) return [];

        $result = $this->request('POST', "/collections/{$this->collection}/points/scroll", [
            'filter' => [
                'should' => array_map(
                    fn($n) => ['key' => 'name', 'match' => ['value' => $n]],
                    $callNames
                ),
            ],
            'with_payload' => true,
            'limit' => 50,
        ]);
        return $result['result']['points'] ?? [];
    }

    private function request(string $method, string $path, array $body = []): array
    {
        $ch = curl_init($this->url . $path);
        $opts = [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CUSTOMREQUEST => $method,
            CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
            CURLOPT_TIMEOUT => 30,
        ];
        if ($body) {
            $opts[CURLOPT_POSTFIELDS] = json_encode($body);
        }
        curl_setopt_array($ch, $opts);
        $result = curl_exec($ch);
        curl_close($ch);
        return json_decode($result, true) ?? [];
    }
}
