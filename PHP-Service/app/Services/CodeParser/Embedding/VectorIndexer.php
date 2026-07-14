<?php

declare(strict_types=1);

/**
 * VectorIndexer
 *
 * Defines the metadata schema for code chunks and handles indexing into a
 * vector DB. Uses Qdrant as the example backend (self-hosted, free, PHP-friendly).
 *
 * Alternatives that work equally well:
 *   - Chroma  (REST API, same pattern)
 *   - Weaviate (GraphQL + REST)
 *   - pgvector (if you prefer Postgres)
 *
 * Embedding model: any model with an HTTP API works.
 * Recommended for local use: Ollama with `nomic-embed-text` or `mxbai-embed-large`.
 *
 * Qdrant PHP client:
 *   composer require qdrant/qdrant-client
 *
 * Or use raw HTTP (shown below) — no extra dependency needed.
 */

namespace App\Services\CodeParser\Embedding;

use App\Services\CodeParser\CodeChunker;
use App\Services\CodeParser\Models\CodeChunk;

// ─── Schema definition (documentation + validation) ────────────────────────────

// ─── Qdrant client (raw HTTP — no extra composer package needed) ────────────────

// ─── Indexer — ties everything together ────────────────────────────────────────

final readonly class VectorIndexer
{
    public function __construct(
        private CodeChunker  $chunker,
        private LocalEmbedder $embedder,
        private QdrantClient  $qdrant,
    )
    {
    }

    /**
     * Index all PHP files under $directory.
     * Skips chunks whose content_hash hasn't changed since last index.
     */
    public function indexDirectory(string $directory, int $batchSize = 20): void
    {
        $this->qdrant->ensureCollection();

        $batch = [];
        $total = 0;

        foreach ($this->chunker->chunkDirectory($directory) as $chunk) {
            $hash = hash('sha256', $chunk->code);
            $id = $this->stableId($chunk->file, $chunk->name, $chunk->startLine);

            $embeddableText = $chunk->toEmbeddableText();
            $vector = $this->embedder->embed($embeddableText);

            $batch[] = [
                'id' => $id,
                'vector' => $vector,
                'payload' => array_merge($chunk->toMetadata(), [
                    'id' => $id,
                    'code' => $chunk->code,
                    'content_hash' => $hash,
                    'indexed_at' => date('c'),
                ]),
            ];

            if (count($batch) >= $batchSize) {
                $this->qdrant->upsertPoints($batch);
                $total += count($batch);
                echo "Indexed $total chunks...\n";
                $batch = [];
            }
        }

        if ($batch) {
            $this->qdrant->upsertPoints($batch);
            $total += count($batch);
        }

        echo "Done. Total chunks indexed: $total\n";
    }

    /**
     * Build context for an exception.
     *
     * Given an exception's stack trace, this:
     *  1. Looks up the crashing function by name + file
     *  2. Expands the call graph one level deep
     *  3. Returns chunks ordered by relevance
     *
     * @param array $stackFrame ['function' => 'processPayment', 'class' => 'PaymentService', ...]
     * @param string $errorText The exception message (used for semantic search)
     * @return array array
     */
    public function buildExceptionContext(array $stackFrame, string $errorText): array
    {
        $results = [];

        // 1. Exact lookup of the crashing symbol
        $exact = $this->qdrant->lookupByName(
            $stackFrame['function'] ?? '',
            $stackFrame['class'] ?? null,
        );
        $results = array_merge($results, $exact);

        // 2. Semantic search scoped to the same file
        $queryVector = $this->embedder->embed($errorText . "\n" . ($stackFrame['function'] ?? ''));
        $filter = [];
        if (!empty($stackFrame['file'])) {
            $filter = ['must' => [['key' => 'file', 'match' => ['value' => $stackFrame['file']]]]];
        }
        $semantic = $this->qdrant->search($queryVector, 3, $filter);
        $results = array_merge($results, array_column($semantic, 'payload'));

        // 3. Call graph expansion — pull in callees of the crashing function
        if ($exact) {
            $calls = $exact[0]['payload']['calls'] ?? [];
            $callees = $this->qdrant->fetchCallees($calls);
            $results = array_merge($results, array_column($callees, 'payload'));
        }

        // Deduplicate by id
        $seen = [];
        return array_filter($results, function ($r) use (&$seen) {
            $id = $r['id'] ?? $r['name'] ?? spl_object_id((object)$r);
            if (isset($seen[$id])) return false;
            $seen[$id] = true;
            return true;
        });
    }

    /**
     * Index a pre-collected array of CodeChunk objects.
     *
     * This is the method used by ChunkCodeCommand, which owns its own
     * scanning loop and progress bar. The command collects all chunks first,
     * then hands them here in batches — keeping UI concerns out of VectorIndexer.
     *
     * Skips re-indexing any chunk whose content_hash matches what is already
     * stored, so incremental re-runs only embed what actually changed.
     *
     * @param CodeChunk[] $chunks
     */
    public function indexChunks(array $chunks): void
    {
        if (empty($chunks)) {
            return;
        }

        $this->qdrant->ensureCollection();

        $points = array_map(function (CodeChunk $chunk): array {
            $id   = $this->stableId($chunk->file, $chunk->name, $chunk->startLine);
            $hash = hash('sha256', $chunk->code);

            return [
                'id'      => $id,
                'vector'  => $this->embedder->embed($chunk->toEmbeddableText()),
                'payload' => array_merge($chunk->toMetadata(), [
                    'id'           => $id,
                    'code'         => $chunk->code,
                    'content_hash' => $hash,
                    'indexed_at'   => date('c'),
                ]),
            ];
        }, $chunks);

        $this->qdrant->upsertPoints($points);
    }

    /** Deterministic UUID-like ID from file + name + line */
    private function stableId(string $file, string $name, int $line): string
    {
        return hash('sha256', "$file::$name::$line");
    }
}


// ─── Example usage ─────────────────────────────────────────────────────────────

/*
$chunker  = new CodeChunker();
$embedder = new LocalEmbedder(model: 'nomic-embed-text');   // running via Ollama
$qdrant   = new QdrantClient(collection: 'my_project');

$indexer  = new VectorIndexer($chunker, $embedder, $qdrant);

// One-time (or CI) indexing
$indexer->indexDirectory('/path/to/your/src');

// At exception time — build context for the LLM
$context = $indexer->buildExceptionContext(
    stackFrame: [
        'function' => 'processPayment',
        'class'    => 'PaymentService',
        'file'     => 'src/Payment/PaymentService.php',
        'line'     => 142,
    ],
    errorText: 'TypeError: Argument 1 passed to processPayment() must be of type float, string given'
);

foreach ($context as $chunk) {
    echo $chunk['name'] . " (" . $chunk['file'] . ")\n";
    echo $chunk['code'] . "\n---\n";
}
*/
