<?php

declare(strict_types=1);

namespace App\Services\CodeParser;

use App\Services\CodeParser\Models\CodeChunk;
use Symfony\Component\Yaml\Yaml;

/**
 * ChunkExporter
 *
 * Serializes CodeChunk arrays to JSON or YAML files.
 *
 * JSON: built-in, no extra dependency.
 * YAML: uses symfony/yaml, which is already a Laravel dependency.
 *
 * The exported file includes a metadata envelope so the file is
 * self-describing and can be re-imported or diffed in CI.
 *
 * Example JSON output structure:
 * {
 *   "meta": {
 *     "generated_at": "2024-11-01T12:00:00+00:00",
 *     "autopatch_version": "1.0",
 *     "total_chunks": 142,
 *     "total_files": 38
 *   },
 *   "chunks": [ { ...CodeChunk metadata... }, ... ]
 * }
 *
 * Example YAML output structure:
 * meta:
 *   generated_at: '2024-11-01T12:00:00+00:00'
 *   autopatch_version: '1.0'
 *   total_chunks: 142
 *   total_files: 38
 * chunks:
 *   - type: method
 *     name: processPayment
 *     class: PaymentService
 *     ...
 */
final class ChunkExporter
{
    private const AUTOPATCH_VERSION = '1.0';

    // ─── Public API ────────────────────────────────────────────────────────────

    /**
     * Write chunks to a JSON file.
     *
     * @param  CodeChunk[] $chunks
     * @param  string      $absolutePath  Full filesystem path (directory must exist)
     * @throws \RuntimeException|\JsonException          On write failure
     */
    public function toJsonFile(array $chunks, string $absolutePath): void
    {
        $payload = $this->buildEnvelope($chunks);

        $json = json_encode(
            $payload,
            JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR
        );

        $this->writeFile($absolutePath, $json);
    }

    /**
     * Write chunks to a YAML file.
     *
     * @param  CodeChunk[] $chunks
     * @param  string      $absolutePath  Full filesystem path (directory must exist)
     * @throws \RuntimeException          On write failure
     */
    public function toYamlFile(array $chunks, string $absolutePath): void
    {
        $payload = $this->buildEnvelope($chunks);

        // inline depth 4: scalars and short arrays stay on one line,
        // deeper structures (parameters array of objects) expand fully.
        $yaml = Yaml::dump(
            $payload,
            inline: 4,
            indent: 2,
            flags: Yaml::DUMP_MULTI_LINE_LITERAL_BLOCK    // code blocks use literal | style
                 | Yaml::DUMP_EMPTY_ARRAY_AS_SEQUENCE      // empty arrays → []
        );

        // Prepend a file-level comment so it's clear what generated this
        $header = implode("\n", [
            '# Autopatch — code chunk export',
            '# Generated: ' . now()->toIso8601String(),
            '# Do not edit manually.',
            '',
        ]);

        $this->writeFile($absolutePath, $header . $yaml);
    }

    /**
     * Write chunks to a plain text file using each chunk's toEmbeddableText().
     *
     * This is the format you pass directly to a local LLM — no JSON syntax
     * tokens, no structural noise. Each chunk is rendered as a self-contained
     * block separated by a divider so the model can clearly distinguish where
     * one function ends and the next begins.
     *
     * Output structure:
     *
     *   ================================================================
     *   [1/142] PaymentService::charge()  •  src/Services/PaymentService.php:42
     *   ================================================================
     *   /** docblock ... *\/
     *   Class: App\Services\PaymentService
     *   Signature: function charge(float $amount, string $currency): Receipt
     *   Calls: App\Repositories\PaymentRepository->[save] | __function->[now]
     *
     *   public function charge(float $amount, string $currency): Receipt
     *   {
     *       ...
     *   }
     *
     *
     * @param  CodeChunk[] $chunks
     * @param  string      $absolutePath  Full filesystem path (directory must exist)
     * @throws \RuntimeException          On write failure
     */
    public function toTextFile(array $chunks, string $absolutePath): void
    {
        $parts = [];

        // File-level header
        $parts[] = implode("\n", [
            'Autopatch — embeddable text export',
            'Generated: ' . now()->toIso8601String(),
            'Total chunks: ' . count($chunks),
            'Format: toEmbeddableText() — optimised for LLM context windows',
            str_repeat('=', 64),
            '',
        ]);

        $total = count($chunks);

        foreach ($chunks as $index => $chunk) {
            $position = $index + 1;
            $label    = $chunk->className
                ? "{$chunk->className}::{$chunk->name}()"
                : "{$chunk->name}()";
            $location = "{$chunk->file}:{$chunk->startLine}";

            // ── Chunk header ──────────────────────────────────────────────
            $parts[] = str_repeat('=', 64);
            $parts[] = "[{$position}/{$total}] {$label}  •  {$location}";
            $parts[] = str_repeat('=', 64);

            // ── Embeddable text body ──────────────────────────────────────
            // toEmbeddableText() returns: docblock + structured header + code.
            // This is exactly what the embedding model and the LLM should see.
            $parts[] = $chunk->toEmbeddableText();

            // ── Separator between chunks ──────────────────────────────────
            $parts[] = '';
            $parts[] = '';
        }

        $this->writeFile($absolutePath, implode("\n", $parts));
    }

    /**
     * Return the envelope as a plain array (useful for testing or piping
     * to a custom serializer without touching the filesystem).
     *
     * @param  CodeChunk[] $chunks
     */
    public function toArray(array $chunks): array
    {
        return $this->buildEnvelope($chunks);
    }

    // ─── Serialization ─────────────────────────────────────────────────────────

    /**
     * Build the full envelope: metadata header + serialized chunks.
     *
     * @param  CodeChunk[] $chunks
     */
    private function buildEnvelope(array $chunks): array
    {
        $serialized = array_map(fn(CodeChunk $c) => $this->serializeChunk($c), $chunks);

        $uniqueFiles = count(array_unique(array_column($serialized, 'file')));

        return [
            'meta'   => [
                'generated_at'      => now()->toIso8601String(),
                'autopatch_version' => self::AUTOPATCH_VERSION,
                'total_chunks'      => count($chunks),
                'total_files'       => $uniqueFiles,
            ],
            'chunks' => $serialized,
        ];
    }

    /**
     * Serialize a single CodeChunk to a plain array.
     *
     * The `code` field is included here — it's excluded from the vector DB
     * metadata by default but valuable in an export for diffing and review.
     *
     * Fields are ordered deliberately:
     *   identity fields first → location → signature → relationships → content
     */
    private function serializeChunk(CodeChunk $chunk): array
    {
        return [
            // Identity
            'type'        => $chunk->type,
            'name'        => $chunk->name,
            'class'       => $chunk->className,

            // Location
            'file'        => $chunk->file,
            'start_line'  => $chunk->startLine,
            'end_line'    => $chunk->endLine,

            // Signature
            'modifiers'   => $chunk->modifiers,
            'parameters'  => $chunk->parameters,
            'return_type' => $chunk->returnType,

            // Relationships
            'calls'       => $chunk->calls,

            // Documentation
            'has_docblock' => $chunk->docblock !== null,
            'docblock'     => $chunk->docblock,

            // Source (included in export, not in vector DB payload)
            'code'         => $chunk->code,
        ];
    }

    // ─── File I/O ──────────────────────────────────────────────────────────────

    /**
     * Write content to a file atomically:
     * write to a temp file first, then rename — prevents corrupt output
     * if the process is interrupted mid-write.
     *
     * @throws \RuntimeException
     */
    private function writeFile(string $absolutePath, string $content): void
    {
        $tmpPath = $absolutePath . '.tmp.' . uniqid('', true);

        try {
            if (file_put_contents($tmpPath, $content, LOCK_EX) === false) {
                throw new \RuntimeException("Could not write to temporary file: {$tmpPath}");
            }

            if (!rename($tmpPath, $absolutePath)) {
                throw new \RuntimeException(
                    "Could not move temp file to destination: {$absolutePath}"
                );
            }
        } catch (\Throwable $e) {
            // Clean up the temp file if it exists
            if (file_exists($tmpPath)) {
                @unlink($tmpPath);
            }
            throw $e;
        }
    }
}
