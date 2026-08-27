<?php

declare(strict_types=1);

namespace App\Console;

use App\CodeParser\CodeChunker;
use App\CodeParser\Models\CodeChunk;
use App\CodeParser\WorkspaceTypeScanner;
use Throwable;

/**
 * Pure-PHP replacement for the former Laravel `code:parse` artisan command.
 *
 * Parses PHP files into AST chunks and prints them to stdout.
 *
 * Usage:
 *   php bin/code-parse --path=/abs/project
 *   php bin/code-parse --path=/abs/project --output=json
 *   php bin/code-parse --path=/abs/file.php --output=text
 *
 * Exit codes: 0 = ok, 1 = nothing to scan / bad args, 2 = partial parse failure.
 */
final class CodeParseCommand
{
    private int $totalChunks  = 0;
    private int $totalFiles   = 0;
    private int $skippedFiles = 0;
    private array $errors     = [];

    public function run(array $argv): int
    {
        $opts = $this->parseArgs($argv);

        $path = $opts['path'] ?? null;
        if ($path === null || trim($path) === '') {
            fwrite(STDERR, "Error: --path=<dir-or-file> is required.\n");
            fwrite(STDERR, "Usage: php bin/code-parse --path=<path> [--output=json|text|summary|none] [--type-map-file=FILE]\n");
            return 1;
        }

        $real = realpath($path);
        if ($real === false || !file_exists($real)) {
            fwrite(STDERR, "Error: path does not exist: {$path}\n");
            return 1;
        }

        $output = $opts['output'] ?? 'json';
        if (!in_array($output, ['json', 'text', 'summary', 'none'], true)) {
            fwrite(STDERR, "Error: --output must be json, text, summary, or none (got: {$output}).\n");
            return 1;
        }

        // External type map (from a prior WorkspaceTypeScanner pass), if given.
        $typeMap = [];
        if (isset($opts['type-map-file'])) {
            $file = $opts['type-map-file'];
            if (is_file($file)) {
                $typeMap = json_decode((string) file_get_contents($file), true) ?: [];
            }
        }

        $chunker = new CodeChunker();

        // When scanning a whole workspace, build the type map from it directly
        // so Eloquent magic properties / @property annotations resolve cleanly.
        if (is_dir($real) && empty($typeMap)) {
            try {
                $typeMap = (new WorkspaceTypeScanner())->scan($real);
            } catch (Throwable $e) {
                fwrite(STDERR, "Warning: type scanner failed: " . $e->getMessage() . "\n");
            }
        }

        $chunks = is_file($real)
            ? $this->chunkSingleFile($chunker, $real, basename($real), $typeMap)
            : $this->chunkDirectory($chunker, $real, $typeMap);

        if (empty($chunks)) {
            if ($output === 'json') {
                echo "[]\n";
            }
            fwrite(STDERR, "No chunks produced for: {$real}\n");
            return 1;
        }

        switch ($output) {
            case 'json':
                $data = array_map(fn(CodeChunk $c) => $c->toMetadata(), $chunks);
                echo json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
                echo "\n";
                break;

            case 'text':
                $this->renderText($chunks);
                break;

            case 'summary':
                $this->renderSummary($chunks);
                break;

            case 'none':
            default:
                break;
        }

        if (!empty($this->errors)) {
            fwrite(STDERR, count($this->errors) . " file(s) had parse errors:\n");
            foreach ($this->errors as $err) {
                fwrite(STDERR, "  {$err['file']}: {$err['message']}\n");
            }
            return 2;
        }

        return 0;
    }

    /** @return CodeChunk[] */
    private function chunkDirectory(CodeChunker $chunker, string $directory, array $typeMap): array
    {
        $files = $this->collectPhpFiles($directory);
        $chunks = [];
        foreach ($files as $file) {
            $relative = ltrim(str_replace($directory, '', $file), DIRECTORY_SEPARATOR);
            $chunks = array_merge($chunks, $this->chunkSingleFile($chunker, $file, $relative, $typeMap));
        }
        return $chunks;
    }

    /** @return CodeChunk[] */
    private function chunkSingleFile(CodeChunker $chunker, string $absolute, string $relative, array $typeMap): array
    {
        try {
            $chunks = $chunker->chunkFile($absolute, $relative, $typeMap);
            $this->totalFiles++;
            $this->totalChunks += count($chunks);
            return $chunks;
        } catch (Throwable $e) {
            $this->skippedFiles++;
            $this->errors[] = ['file' => $relative ?: basename($absolute), 'message' => $e->getMessage()];
            return [];
        }
    }

    /** @return string[] */
    private function collectPhpFiles(string $directory): array
    {
        $iterator = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($directory, \FilesystemIterator::SKIP_DOTS)
        );

        $files = [];
        foreach ($iterator as $file) {
            if ($file->getExtension() === 'php') {
                $files[] = $file->getPathname();
            }
        }

        sort($files);
        return $files;
    }

    /** @param CodeChunk[] $chunks */
    private function renderText(array $chunks): void
    {
        $total = count($chunks);
        foreach ($chunks as $index => $chunk) {
            $position = $index + 1;
            $label = $chunk->className
                ? "{$chunk->className}::{$chunk->name}()"
                : "{$chunk->name}()";
            $location = "{$chunk->file}:{$chunk->startLine}";

            echo str_repeat('=', 64) . "\n";
            echo "[{$position}/{$total}] {$label}  •  {$location}\n";
            echo str_repeat('=', 64) . "\n";
            echo $chunk->toEmbeddableText() . "\n\n";
        }
    }

    /** @param CodeChunk[] $chunks */
    private function renderSummary(array $chunks): void
    {
        $byType = array_count_values(array_map(fn($c) => $c->type, $chunks));
        $byClass = [];
        foreach ($chunks as $c) {
            if ($c->className) {
                $byClass[$c->className] = ($byClass[$c->className] ?? 0) + 1;
            }
        }
        arsort($byClass);

        echo "Chunks by type:\n";
        foreach ($byType as $type => $count) {
            echo "  {$type}: {$count}\n";
        }
        echo "\nTop classes by chunk count:\n";
        foreach (array_slice($byClass, 0, 10, true) as $class => $count) {
            echo "  {$class}: {$count}\n";
        }
    }

    private function parseArgs(array $argv): array
    {
        $opts = [];
        $args = array_slice($argv, 1);
        for ($i = 0; $i < count($args); $i++) {
            $arg = $args[$i];
            if (str_starts_with($arg, '--') && str_contains($arg, '=')) {
                [$key, $value] = explode('=', $arg, 2);
                $opts[substr($key, 2)] = $value;
            } elseif (str_starts_with($arg, '--') && isset($args[$i + 1])) {
                // Support `--path <value>` as well as `--path=<value>`.
                $opts[substr($arg, 2)] = $args[++$i];
            }
        }
        return $opts;
    }
}
