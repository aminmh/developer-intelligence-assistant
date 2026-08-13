<?php

declare(strict_types=1);

namespace App\MCP\Tools;

use Illuminate\Support\Facades\Artisan;
use Illuminate\Support\Facades\File;
use Throwable;

final class ExportStructureTool
{
    public function handle(
        ?string $workspace_id = null,
        ?string $filename = null,
        ?string $path = null,
        int $depth = 4,
        bool $include_hidden = false,
    ): string|array {
        $args = [
            '--output' => storage_path('app/rag/project-structure.md'),
            '--depth' => $depth,
        ];

        if ($include_hidden) {
            $args['--include-hidden'] = true;
        }

        // Priority: workspace_id > filename > path > base_path
        if ($workspace_id) {
            $args['--workspace-id'] = $workspace_id;
        } elseif ($filename) {
            $args['--storage-filename'] = $filename;
        } elseif ($path) {
            $args['path'] = $path;
        }

        try {
            $outputPath = storage_path('app/rag/project-structure.md');
            $exitCode = Artisan::call('project:export-structure', $args);

            if ($exitCode !== 0) {
                return ['success' => false, 'exit_code' => $exitCode, 'error' => trim(Artisan::output())];
            }

            // Return the generated Markdown content (capped for LLM context)
            if (!File::exists($outputPath)) {
                return ['success' => false, 'error' => 'Output file not found'];
            }

            $content = File::get($outputPath);
            $totalLen = strlen($content);
            $maxLen = 8000; // Cap to ~2K tokens for LLM context window

            if ($totalLen > $maxLen) {
                $truncPos = strrpos(substr($content, 0, $maxLen), "\n");
                $content = ($truncPos !== false ? substr($content, 0, $truncPos) : substr($content, 0, $maxLen))
                    . "\n\n... (truncated, showing "
                    . number_format(($truncPos ?: $maxLen) / $totalLen * 100, 0)
                    . "% of " . number_format($totalLen) . " total bytes)"
                    . "\nFull output: " . $outputPath;
            }

            return $content;
        } catch (Throwable $e) {
            return [
                'success' => false,
                'error' => $e->getMessage(),
            ];
        }
    }
}