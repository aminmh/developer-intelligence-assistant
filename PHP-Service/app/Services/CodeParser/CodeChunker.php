<?php

declare(strict_types=1);

/**
 * CodeChunker
 *
 * Parses PHP source files into semantically complete chunks using nikic/php-parser.
 * Each chunk maps to one function or method — never a partial slice.
 *
 * Install dependency:
 *   composer require nikic/php-parser
 *
 * If you prefer the Tree-sitter CLI as a sidecar (language-agnostic), see
 * the treeSitterChunk() method at the bottom of this file.
 */

namespace App\Services\CodeParser;

use App\Services\CodeParser\Models\CodeChunk;
use PhpParser\Error;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitor\NameResolver;
use PhpParser\ParserFactory;
use PhpParser\PrettyPrinter\Standard as PrettyPrinter;

// ─── Data shape ────────────────────────────────────────────────────────────────


// ─── Visitor that collects nodes ────────────────────────────────────────────────

// ─── Public API ─────────────────────────────────────────────────────────────────

final readonly class CodeChunker
{
    private \PhpParser\Parser $parser;
    private PrettyPrinter $printer;

    public function __construct()
    {
        $this->parser = (new ParserFactory())->createForNewestSupportedVersion();
        $this->printer = new PrettyPrinter();
    }

    /**
     * Parse a single PHP file and return all function/method chunks.
     *
     * @return CodeChunk[]
     */
    public function chunkFile(string $absolutePath, string $relativePath = '', array $typeMap = []): array
    {
        $source = file_get_contents($absolutePath);
        if ($source === false) {
            throw new \RuntimeException("Cannot read file: $absolutePath");
        }

        return $this->chunkSource($source, $relativePath ?: $absolutePath, $typeMap);
    }

    /**
     * Parse raw PHP source code.
     *
     * @return CodeChunk[]
     */
    public function chunkSource(string $source, string $filePath = '', array $typeMap = []): array
    {
        try {
            $ast = $this->parser->parse($source);
        } catch (Error $e) {
            // Syntax error — return empty; log upstream if needed
            return [];
        }

        $collector = new \App\Services\CodeParser\Models\ChunkCollector($source, $filePath, $this->printer, $typeMap);
        $traverser = new NodeTraverser();
        $traverser->addVisitor(new NameResolver());
        $traverser->addVisitor($collector);
        $traverser->traverse($ast);

        return $collector->chunks;
    }

    /**
     * Recursively chunk all .php files under a directory.
     *
     * @return \Generator<CodeChunk>
     */
    public function chunkDirectory(string $rootPath, array $typeMap = []): \Generator
    {
        $iterator = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($rootPath, \FilesystemIterator::SKIP_DOTS)
        );

        foreach ($iterator as $file) {
            if ($file->getExtension() !== 'php') continue;

            $abs = $file->getPathname();
            $relative = ltrim(str_replace($rootPath, '', $abs), DIRECTORY_SEPARATOR);

            foreach ($this->chunkFile($abs, $relative, $typeMap) as $chunk) {
                yield $chunk;
            }
        }
    }

    // ── Tree-sitter sidecar (language-agnostic alternative) ──────────────────
    //
    // Use this when you want to chunk non-PHP files in the same pipeline
    // (e.g. JS, Python, Go). Requires the tree-sitter CLI to be installed:
    //   npm install -g tree-sitter-cli
    //
    public function treeSitterChunk(string $filePath, string $language = 'php'): array
    {
        // tree-sitter parse outputs a lisp-like tree to stdout
        $cmd = sprintf('tree-sitter parse --scope source.%s %s 2>/dev/null', $language, escapeshellarg($filePath));
        $output = shell_exec($cmd);

        if (!$output) return [];

        // Extract function definition line ranges from the tree-sitter output.
        // The output format looks like: (function_definition [3, 0] - [12, 1])
        $chunks = [];
        $source = file_get_contents($filePath);
        $lines = explode("\n", $source);

        preg_match_all('/\(function_definition \[(\d+), \d+\] - \[(\d+), \d+\]/', $output, $matches, PREG_SET_ORDER);

        foreach ($matches as $m) {
            $start = (int)$m[1];  // tree-sitter is 0-indexed
            $end = (int)$m[2];
            $code = implode("\n", array_slice($lines, $start, $end - $start + 1));

            $chunks[] = [
                'code' => $code,
                'file' => $filePath,
                'start_line' => $start + 1,
                'end_line' => $end + 1,
                'language' => $language,
            ];
        }

        return $chunks;
    }
}


// ─── Example usage ─────────────────────────────────────────────────────────────

/*
$chunker = new CodeChunker();

// Single file
$chunks = $chunker->chunkFile('/path/to/src/PaymentService.php', 'src/PaymentService.php');

foreach ($chunks as $chunk) {
    echo $chunk->name . " (" . $chunk->startLine . "-" . $chunk->endLine . ")\n";
    echo "Calls: " . implode(', ', $chunk->calls) . "\n";
    echo "---\n";
    // Pass to your embedder:
    $text     = $chunk->toEmbeddableText();
    $metadata = $chunk->toMetadata();
}

// Whole directory
foreach ($chunker->chunkDirectory('/path/to/src') as $chunk) {
    $embeddableText = $chunk->toEmbeddableText();
    $metadata       = $chunk->toMetadata();
    // → send to your vector DB
}
*/
