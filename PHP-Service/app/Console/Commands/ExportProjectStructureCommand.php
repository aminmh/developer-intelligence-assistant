<?php

declare(strict_types=1);

namespace App\Console\Commands;

use Illuminate\Console\Command;
use Illuminate\Support\Facades\File;
use Illuminate\Support\Facades\Storage;
use Symfony\Component\Process\Process;
use Throwable;

final class ExportProjectStructureCommand extends Command
{
    protected $signature = 'project:export-structure
        {path? : Project directory path (default: base path)}
        {--output= : Output Markdown file path}
        {--depth=4 : Maximum directory depth}
        {--include-hidden : Include hidden files and directories}
        {--storage-filename= : Download ZIP from MinIO (tus-uploads) by filename before scanning}
        {--workspace-id= : Use pre-extracted workspace at /{configured workspace disk}/project/id}';

    protected $description = 'Export a RAG-friendly Markdown project directory structure using tree';

    private ?string $tempExtractPath = null;

    public function handle(): int
    {
        $projectPath = $this->resolveProjectPath();
        if ($projectPath === null) {
            return self::FAILURE;
        }

        $maxDepth = max(1, (int) $this->option('depth'));
        $outputFile = (string) ($this->option('output')
            ?: storage_path('app/rag/project-structure.md'));

        $excludedPatterns = [
            'vendor', 'node_modules', '.git', '.idea', '.vscode', '.DS_Store',
            'storage/framework', 'storage/logs', 'bootstrap/cache',
            'public/build', 'public/hot', 'var/cache', 'var/log',
            'coverage', 'dist', 'build', '*.log', '*.lock',
        ];

        $command = ['tree', '-F', '-I', implode('|', $excludedPatterns),
                     '-L', (string) $maxDepth];

        if ($this->option('include-hidden')) {
            $command[] = '-a';
        }

        $command[] = $projectPath;

        try {
            $this->info("Scanning: {$projectPath}");

            $process = new Process($command);
            $process->setTimeout(60);
            $process->run();

            if (!$process->isSuccessful()) {
                $this->error('Unable to run the tree command.');
                $this->line($process->getErrorOutput());
                return self::FAILURE;
            }

            $treeOutput = trim($process->getOutput());

            $markdown = $this->buildMarkdown(
                projectPath: $projectPath,
                maxDepth: $maxDepth,
                excludedPatterns: $excludedPatterns,
                treeOutput: $treeOutput,
            );

            $directory = dirname($outputFile);
            if (!is_dir($directory) && !mkdir($directory, 0775, true) && !is_dir($directory)) {
                $this->error("Could not create output directory: {$directory}");
                return self::FAILURE;
            }

            file_put_contents($outputFile, $markdown);

            $this->info("Structure exported successfully: {$outputFile}");

            return self::SUCCESS;
        } catch (Throwable $e) {
            $this->error($e->getMessage());
            return self::FAILURE;
        } finally {
            // Cleanup temp extraction if we downloaded from MinIO
            if (isset($this->tempExtractPath) && File::exists($this->tempExtractPath)) {
                File::deleteDirectory($this->tempExtractPath);
            }
        }
    }

    private function resolveProjectPath(): ?string
    {
        // 1. Pre-extracted workspace (fastest)
        if ($workspaceId = $this->option('workspace-id')) {
            $wsPath = Storage::disk('workspace')->path('/project/' . $workspaceId);
            if (!is_dir($wsPath)) {
                $this->error("Workspace not found: {$wsPath}");
                return null;
            }
            return realpath($wsPath) ?: $wsPath;
        }

        // 2. Download ZIP from MinIO by filename
        if ($storageFilename = $this->option('storage-filename')) {
            $disk = Storage::disk('tus_uploads');
            $files = $disk->allFiles('rag-uploads');
            $s3Path = null;
            foreach ($files as $f) {
                if (basename($f) === $storageFilename) {
                    $s3Path = $f;
                    break;
                }
            }

            if (!$s3Path) {
                $this->error("File not found in MinIO: {$storageFilename}");
                return null;
            }

            $tmpZip = tempnam(sys_get_temp_dir(), 'es_zip_');
            file_put_contents($tmpZip, $disk->get($s3Path));

            $this->tempExtractPath = sys_get_temp_dir() . '/es_extract_' . bin2hex(random_bytes(8));
            File::makeDirectory($this->tempExtractPath, 0755, true);

            $zip = new \ZipArchive();
            if ($zip->open($tmpZip) !== true) {
                @unlink($tmpZip);
                $this->error('Could not open ZIP file.');
                return null;
            }

            // ZipSlip protection
            for ($i = 0; $i < $zip->numFiles; $i++) {
                $entry = $zip->getNameIndex($i);
                if (str_contains($entry, '..') || str_starts_with($entry, '/')) {
                    $zip->close();
                    @unlink($tmpZip);
                    $this->error('Security violation in ZIP paths.');
                    return null;
                }
            }

            $zip->extractTo($this->tempExtractPath);
            $zip->close();
            @unlink($tmpZip);

            return realpath($this->tempExtractPath) ?: $this->tempExtractPath;
        }

        // 3. Local filesystem path
        $path = $this->argument('path') ?: base_path();
        $real = realpath((string) $path);
        if ($real === false || !is_dir($real)) {
            $this->error("Path does not exist: {$path}");
            return null;
        }
        return $real;
    }

    /**
     * @param list<string> $excludedPatterns
     */
    private function buildMarkdown(
        string $projectPath,
        int $maxDepth,
        array $excludedPatterns,
        string $treeOutput,
    ): string {
        $excluded = implode(', ', array_map(
            static fn (string $pattern): string => "`{$pattern}`",
            $excludedPatterns,
        ));
        $timestamp = now()->toIso8601String();

        return <<<MARKDOWN
# Project Structure Blueprint

## Metadata

- **Project path:** `{$projectPath}`
- **Generated at:** `{$timestamp}`
- **Maximum depth:** `{$maxDepth}`
- **Excluded patterns:** {$excluded}

## Directory Tree
```text
{$treeOutput}
```
MARKDOWN;
    }
}
