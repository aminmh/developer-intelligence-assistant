<?php

declare(strict_types=1);

namespace App\Services\CodeParser;

use RuntimeException;
use Throwable;

final class CodeParseService
{
    /**
     * @param callable(array):void|null $onProgress
     */
    public function run(ParseOptions $options, ?callable $onProgress = null): array
    {
        $startedAt = microtime(true);

        $files = $this->resolveFiles($options);

        $totalFiles = count($files);
        $parsedFiles = 0;
        $chunksCount = 0;

        $this->emit($onProgress, [
            'event' => 'started',
            'stage' => 'bootstrap',
            'current' => 0,
            'total' => $totalFiles,
            'percent' => 0,
            'message' => 'Parsing started',
        ]);

        foreach ($files as $index => $file) {
            $current = $index + 1;

            $this->emit($onProgress, [
                'event' => 'progress',
                'stage' => 'parsing',
                'current' => $current,
                'total' => $totalFiles,
                'percent' => $totalFiles > 0 ? (int) floor(($current - 1) / $totalFiles * 100) : 100,
                'file' => $file,
                'message' => "Parsing {$file}",
            ]);

            try {
                $fileChunks = $this->parseFileToChunks($file, $options);
            } catch (Throwable $e) {
                $this->emit($onProgress, [
                    'event' => 'error',
                    'stage' => 'parsing',
                    'current' => $current,
                    'total' => $totalFiles,
                    'percent' => $totalFiles > 0 ? (int) floor($current / $totalFiles * 100) : 100,
                    'file' => $file,
                    'message' => $e->getMessage(),
                ]);

                throw $e;
            }

            $parsedFiles++;
            $chunksCount += count($fileChunks);

            $this->emit($onProgress, [
                'event' => 'file_parsed',
                'stage' => 'parsing',
                'current' => $current,
                'total' => $totalFiles,
                'percent' => $totalFiles > 0 ? (int) floor($current / $totalFiles * 100) : 100,
                'file' => $file,
                'chunks_in_file' => count($fileChunks),
                'chunks_total' => $chunksCount,
                'message' => "Parsed {$file}",
            ]);
        }

        if (! $options->dryRun && ! $options->noIndex) {
            $this->emit($onProgress, [
                'event' => 'progress',
                'stage' => 'indexing',
                'current' => $parsedFiles,
                'total' => $totalFiles,
                'percent' => 100,
                'message' => 'Indexing parsed chunks',
            ]);

            $this->indexChunks();
        }

        $exportedTo = null;

        if ($options->exportPath !== null) {
            $exportedTo = $this->exportResult(
                path: $options->exportPath,
                format: $options->exportFormat ?? 'json',
                payload: [
                    'parsed_files' => $parsedFiles,
                    'chunks' => $chunksCount,
                ],
            );

            $this->emit($onProgress, [
                'event' => 'exported',
                'stage' => 'export',
                'current' => $parsedFiles,
                'total' => $totalFiles,
                'percent' => 100,
                'path' => $exportedTo,
                'message' => "Exported result to {$exportedTo}",
            ]);
        }

        $durationMs = (int) round((microtime(true) - $startedAt) * 1000);

        $result = [
            'success' => true,
            'status' => 'completed',
            'parsed_files' => $parsedFiles,
            'chunks' => $chunksCount,
            'duration_ms' => $durationMs,
            'export_path' => $exportedTo,
            'options' => [
                'path' => $options->path,
                'project' => $options->project,
                'export_path' => $options->exportPath,
                'export_format' => $options->exportFormat,
                'no_index' => $options->noIndex,
                'dry_run' => $options->dryRun,
                'batch' => $options->batch,
            ],
        ];

        $this->emit($onProgress, [
            'event' => 'completed',
            'stage' => 'done',
            'current' => $parsedFiles,
            'total' => $totalFiles,
            'percent' => 100,
            'message' => 'Parsing completed',
            'result' => $result,
        ]);

        return $result;
    }

    /**
     * @return list<string>
     */
    private function resolveFiles(ParseOptions $options): array
    {
        if ($options->project) {
            // TODO: replace with your real project-wide discovery logic
            return [
                base_path('app/Models/User.php'),
                base_path('app/Services/ExampleService.php'),
            ];
        }

        if ($options->path === null || trim($options->path) === '') {
            throw new RuntimeException('Either path or --project must be provided.');
        }

        return [$options->path];
    }

    /**
     * @return list<array<string,mixed>>
     */
    private function parseFileToChunks(string $file, ParseOptions $options): array
    {
        // TODO: replace with your real AST/chunk extraction logic
        return [
            [
                'file' => $file,
                'type' => 'class',
                'name' => 'ExampleClass',
            ],
        ];
    }

    private function indexChunks(): void
    {
        // TODO: replace with your real indexing logic
    }

    /**
     * @param array<string,mixed> $payload
     */
    private function exportResult(string $path, string $format, array $payload): string
    {
        $contents = match ($format) {
            'json' => json_encode($payload, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE),
            'text' => print_r($payload, true),
            'yaml' => function_exists('yaml_emit') ? yaml_emit($payload) : json_encode($payload, JSON_PRETTY_PRINT),
            default => throw new RuntimeException("Unsupported export format: {$format}"),
        };

        if ($contents === false) {
            throw new RuntimeException('Failed to encode export payload.');
        }

        $dir = dirname($path);

        if (! is_dir($dir)) {
            mkdir($dir, 0777, true);
        }

        file_put_contents($path, $contents);

        return $path;
    }

    /**
     * @param callable(array):void|null $onProgress
     * @param array<string,mixed> $payload
     */
    private function emit(?callable $onProgress, array $payload): void
    {
        if ($onProgress !== null) {
            $onProgress($payload);
        }
    }
}
