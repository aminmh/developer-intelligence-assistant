<?php

declare(strict_types=1);

namespace App\MCP\Tools;

use Illuminate\Support\Facades\Artisan;
use Illuminate\Support\Facades\File;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Storage;
use Throwable;

final class ChunkCodeTool
{
    public function handle(
        ?string $workspace_id = null,
        ?string $filename = null,
        ?string $path = null,
        bool $project = false,
        ?string $export_format = null,
        bool $no_index = false,
        bool $dry_run = false,
        int $batch = 20,
    ): array {
        $resolvedPath = $this->resolvePath($workspace_id, $filename, $path);

        if ($resolvedPath === null) {
            return ['success' => false, 'status' => 'failed',
                    'error' => 'No path or filename provided'];
        }

        try {
            // Run WorkspaceTypeScanner to resolve Eloquent magic properties + @property annotations
            $typeMapFile = null;
            try {
                $scanner = new \App\Services\CodeParser\WorkspaceTypeScanner();
                $typeMap = $scanner->scan($resolvedPath);
                if (!empty($typeMap)) {
                    $typeMapFile = tempnam(sys_get_temp_dir(), 'type_map_');
                    file_put_contents($typeMapFile, json_encode($typeMap));
                }
            } catch (\Throwable $e) {
                Log::channel('parse_index')->warning('Type scanner failed: ' . $e->getMessage());
            }

            // Run code:parse Artisan command
            $args = ['--path' => $resolvedPath];
            if ($project) $args['--project'] = true;
            if ($export_format) $args['--export-format'] = $export_format;
            if ($no_index) $args['--no-index'] = true;
            if ($dry_run) $args['--dry-run'] = true;
            if ($batch) $args['--batch'] = $batch;
            $args['--output'] = 'json';
            if ($typeMapFile) $args['--type-map-file'] = $typeMapFile;

            $exitCode = Artisan::call('code:parse', $args);
            $output = Artisan::output();

            if ($exitCode !== 0 && $exitCode !== 2) { // 2 = partial failure, ok
                return ['success' => false, 'status' => 'failed',
                        'exit_code' => $exitCode, 'error' => trim($output)];
            }

            Log::channel('parse_index')->info('MCP chunk_code completed', [
                'path' => $resolvedPath, 'exit' => $exitCode,
            ]);

            return [
                'success' => true, 'status' => 'done',
                'exit_code' => $exitCode,
                'stdout' => $output,
            ];

        } catch (Throwable $e) {
            Log::channel('parse_index')->error('MCP chunk_code failed', [
                'error' => $e->getMessage(),
            ]);
            return ['success' => false, 'status' => 'failed',
                    'error' => $e->getMessage()];
        } finally {
            // Cleanup temp extraction + type map file
            if (isset($this->tempExtractPath) && File::exists($this->tempExtractPath)) {
                File::deleteDirectory($this->tempExtractPath);
            }
            if (isset($typeMapFile) && $typeMapFile && file_exists($typeMapFile)) {
                @unlink($typeMapFile);
            }
        }
    }

    private ?string $tempExtractPath = null;

    private function resolvePath(?string $workspace_id, ?string $filename, ?string $path): ?string
    {
        // 1. Pre-extracted workspace
        if ($workspace_id) {
            $wsPath = Storage::disk('workspace')->path('/project/' . $workspace_id);
            return File::exists($wsPath) ? ($path ? "{$wsPath}/{$path}" : $wsPath) : null;
        }

        // 2. Download ZIP from MinIO by filename
        if ($filename) {
            $disk = Storage::disk('tus_uploads');
            $files = $disk->allFiles('rag-uploads');
            $s3Path = null;
            foreach ($files as $f) {
                if (basename($f) === $filename) { $s3Path = $f; break; }
            }

            if (!$s3Path) return null;

            $tmpZip = tempnam(sys_get_temp_dir(), 'cc_zip_');
            file_put_contents($tmpZip, $disk->get($s3Path));

            $this->tempExtractPath = sys_get_temp_dir() . '/cc_extract_' . bin2hex(random_bytes(8));
            File::makeDirectory($this->tempExtractPath, 0755, true);

            $zip = new \ZipArchive();
            if ($zip->open($tmpZip) !== true) { @unlink($tmpZip); return null; }
            for ($i = 0; $i < $zip->numFiles; $i++) {
                $entry = $zip->getNameIndex($i);
                if (str_contains($entry, '..') || str_starts_with($entry, '/')) {
                    $zip->close(); @unlink($tmpZip); return null;
                }
            }
            $zip->extractTo($this->tempExtractPath);
            $zip->close();
            @unlink($tmpZip);

            return $path ? "{$this->tempExtractPath}/{$path}" : $this->tempExtractPath;
        }

        // 3. Local filesystem path
        if ($path) {
            return File::exists($path) ? $path : null;
        }

        return null;
    }
}
