<?php

declare(strict_types=1);

namespace App\Console\Commands;

use App\Services\CodeParser\ChunkExporter;
use App\Services\CodeParser\CodeChunker;
use App\Services\CodeParser\Embedding\VectorIndexer;
use App\Services\CodeParser\Models\CodeChunk;
use Illuminate\Console\Command;
use Illuminate\Contracts\Filesystem\FileNotFoundException;
use Illuminate\Support\Facades\File;
use Throwable;

/**
 * ChunkCodeCommand
 *
 * Parses PHP source files into AST-based chunks and indexes them into the
 * vector DB for use by Autopatch's exception analysis and refactor assistant.
 *
 * Usage:
 *   # Chunk a specific path (file or directory)
 *   php artisan autopatch:chunk --path=app/Services/PaymentService.php
 *   php artisan autopatch:chunk --path=app/Services
 *
 *   # Chunk the whole project based on autoload paths in composer.json
 *   php artisan autopatch:chunk --project
 *
 *   # Export to JSON at a given path
 *   php artisan autopatch:chunk --project --export-path=storage/autopatch/chunks.json
 *
 *   # Export to YAML at a given path
 *   php artisan autopatch:chunk --path=app/Actions --export-path=storage/autopatch/actions.yaml
 *
 *   # Export as embeddable plain text (uses toEmbeddableText() — ideal for LLM input)
 *   php artisan autopatch:chunk --path=app/Services --export-path=storage/autopatch/chunks.txt
 *   php artisan autopatch:chunk --project --export-format=text --export-path=storage/autopatch/chunks.txt
 *
 *   # Export AND index in one pass
 *   php artisan autopatch:chunk --project --export-path=storage/autopatch/chunks.json
 *
 *   # Dry run — parse and report without indexing or writing files
 *   php artisan autopatch:chunk --project --dry-run
 *
 *   # Skip the vector DB — display only
 *   php artisan autopatch:chunk --path=app/Actions --output=table
 *
 *   # Control batch size for the vector DB upsert
 *   php artisan autopatch:chunk --project --batch=50
 */
class ChunkCodeCommand extends Command
{
    protected $signature = 'code:parse
        {--path=            : Chunk a specific file or directory (relative to base_path())}
        {--project          : Chunk all autoload-psr4 source paths defined in composer.json}
        {--export-path=     : Write results to this file (.json, .yaml/.yml, or .txt)}
        {--export-format=   : Force export format: json | yaml | text (inferred from --export-path extension by default)}
        {--dry-run          : Parse and report chunk counts without writing anything}
        {--output=table     : Console output format: table | json | summary | none}
        {--batch=20         : Number of chunks per vector DB upsert batch}
        {--no-index         : Skip vector DB indexing (export or display only)}
        {--stream           : Stream text output to stdout (implies --export-format=text --no-index)}
        {--type-map-file=   : JSON file from WorkspaceTypeScanner for call resolution}
    ';

    protected $description = 'Parse PHP source files into AST chunks, index them, and/or export to JSON/YAML/text';

    // ─── Exit codes ────────────────────────────────────────────────────────────
    const EXIT_OK              = 0;
    const EXIT_NOTHING_TO_SCAN = 1;
    const EXIT_PARTIAL_FAILURE = 2;
    const EXIT_EXPORT_FAILED   = 3;

    // ─── State ─────────────────────────────────────────────────────────────────
    private int   $totalChunks  = 0;
    private int   $totalFiles   = 0;
    private int   $skippedFiles = 0;
    private array $errors       = [];
    private array $typeMap      = [];

    public function handle(
        CodeChunker   $chunker,
        VectorIndexer $indexer,
        ChunkExporter $exporter,
    ): int {
        $this->newLine();
        $this->components->info('Autopatch — code chunker');
        $this->newLine();

        // ── Validate options ───────────────────────────────────────────────────
        if ($error = $this->validateOptions()) {
            $this->components->error($error);
            $this->newLine();
            return self::EXIT_NOTHING_TO_SCAN;
        }

        // ── Resolve which paths to scan ────────────────────────────────────────
        $paths = $this->resolvePaths();

        if (empty($paths)) {
            $this->components->error('Nothing to scan. Use --path=<dir> or --project.');
            $this->newLine();
            $this->line('  Examples:');
            $this->line('    php artisan autopatch:chunk --path=app/Services');
            $this->line('    php artisan autopatch:chunk --project');
            $this->line('    php artisan autopatch:chunk --project --export-path=storage/autopatch/chunks.json');
            $this->newLine();
            return self::EXIT_NOTHING_TO_SCAN;
        }

        $isDryRun    = (bool) $this->option('dry-run');
        $noIndex     = (bool) $this->option('no-index') || $isDryRun;
        $batchSize   = max(1, (int) $this->option('batch'));
        $exportPath  = $this->option('export-path');
        $consoleOut  = $this->option('output') ?? 'table';
        $stream      = (bool) $this->option('stream');

        // --stream implies text format and no indexing
        if ($stream) {
            $noIndex      = true;
            $exportFormat = 'text';
            $consoleOut   = 'none';
        } else {
            // Infer export format from file extension, allow override via --export-format
            $exportFormat = $this->resolveExportFormat($exportPath);
        }

        // Load external type map from WorkspaceTypeScanner
        $typeMapFile = $this->option('type-map-file');
        if ($typeMapFile && file_exists($typeMapFile)) {
            $this->typeMap = json_decode(file_get_contents($typeMapFile), true) ?: [];
        }

        // ── Show scan plan ─────────────────────────────────────────────────────
        $this->renderScanPlan($paths, $isDryRun, $noIndex, $batchSize, $exportPath, $exportFormat);

        // ── Scan each path ─────────────────────────────────────────────────────
        $allChunks = [];

        foreach ($paths as $label => $absolutePath) {
            $chunks    = $this->scanPath($chunker, $absolutePath, $label);
            $allChunks = array_merge($allChunks, $chunks);
        }
        if (empty($allChunks)) {
            $this->newLine();
            $this->components->warn('No chunks produced. Check that the paths contain .php files.');

            return self::EXIT_NOTHING_TO_SCAN;
        }

        // ── Stream to stdout ────────────────────────────────────────────────────
        if ($stream) {
            $handle = fopen('php://stdout', 'w');
            if (!$handle) {
                $this->components->error('Could not open stdout for streaming.');

                return self::EXIT_EXPORT_FAILED;
            }

            $exporter->toTextStream($allChunks, $handle);
            fclose($handle);

            return self::EXIT_OK;
        }

        // ── Export to file ─────────────────────────────────────────────────────
        if ($exportPath && !$isDryRun) {
            $exported = $this->exportChunks($exporter, $allChunks, $exportPath, $exportFormat);
            if (!$exported) {
                return self::EXIT_EXPORT_FAILED;
            }
        }

        // ── Index into vector DB ───────────────────────────────────────────────
        if (!$noIndex) {
            $this->indexChunks($indexer, $allChunks, $batchSize);
        }

        // ── Console output ─────────────────────────────────────────────────────
        if ($consoleOut !== 'none') {
            $this->renderConsoleOutput($allChunks, $consoleOut);
        }

        // ── Final summary ──────────────────────────────────────────────────────
        $this->renderSummary($noIndex, $isDryRun, $exportPath, $exportFormat);

        if (!empty($this->errors)) {
            $this->newLine();
            $this->components->warn(count($this->errors) . ' file(s) had parse errors:');
            foreach ($this->errors as $err) {
                $this->line("  <fg=yellow>⚠</> {$err['file']}: {$err['message']}");
            }
        }

        $this->newLine();
        return empty($this->errors) ? self::EXIT_OK : self::EXIT_PARTIAL_FAILURE;
    }

    // ─── Validation ────────────────────────────────────────────────────────────

    private function validateOptions(): ?string
    {
        $exportPath   = $this->option('export-path');
        $exportFormat = $this->option('export-format');

        // Must provide at least one scan source
        if (!$this->option('path') && !$this->option('project')) {
            return 'Provide --path=<dir> or --project.';
        }

        // If --export-format is given explicitly, validate it
        if ($exportFormat && !in_array($exportFormat, ['json', 'yaml', 'text'], true)) {
            return "--export-format must be 'json', 'yaml', or 'text', got: {$exportFormat}";
        }

        // If --export-path is given, validate the extension is recognizable
        // (only when --export-format is not overriding it)
        if ($exportPath && !$exportFormat) {
            $ext = strtolower(pathinfo($exportPath, PATHINFO_EXTENSION));
            if (!in_array($ext, ['json', 'yaml', 'yml', 'txt'], true)) {
                return "--export-path extension must be .json, .yaml, .yml, or .txt (got .{$ext}). "
                    . "Or use --export-format to override.";
            }
        }

        return null;
    }

    // ─── Export format resolution ──────────────────────────────────────────────

    /**
     * Determine the export format.
     * Priority: --export-format flag > file extension of --export-path.
     */
    private function resolveExportFormat(?string $exportPath): ?string
    {
        if (!$exportPath) {
            return null;
        }

        // Explicit override wins
        if ($forced = $this->option('export-format')) {
            return strtolower($forced);
        }

        $ext = strtolower(pathinfo($exportPath, PATHINFO_EXTENSION));

        return match ($ext) {
            'json'        => 'json',
            'yaml', 'yml' => 'yaml',
            'txt'         => 'text',
            default       => 'json',
        };
    }

    // ─── Path resolution ───────────────────────────────────────────────────────

    private function resolvePaths(): array
    {
        if ($path = $this->option('path')) {
            $absolute = $this->resolveRelativePath($path);

            if (!file_exists($absolute)) {
                $this->components->error("Path does not exist: {$absolute}");
                return [];
            }

            $label = is_dir($absolute) ? $path : dirname($path);
            return [$label => $absolute];
        }

        if ($this->option('project')) {
            return $this->resolveComposerPaths();
        }

        return [];
    }

    /**
     * Reads composer.json and extracts all PSR-4 autoload source directories.
     * Intentionally excludes autoload-dev (test paths).
     * @throws FileNotFoundException
     */
    private function resolveComposerPaths(): array
    {
        $composerPath = base_path('composer.json');

        if (!File::exists($composerPath)) {
            $this->components->error('composer.json not found at: ' . $composerPath);
            return [];
        }

        $composer = json_decode(File::get($composerPath), true);

        if (json_last_error() !== JSON_ERROR_NONE) {
            $this->components->error('composer.json is not valid JSON: ' . json_last_error_msg());
            return [];
        }

        $psr4Paths     = $composer['autoload']['psr-4']    ?? [];
        $classmapPaths = $composer['autoload']['classmap'] ?? [];

        if (empty($psr4Paths) && empty($classmapPaths)) {
            $this->components->warn('No autoload paths found in composer.json.');
            return [];
        }

        $resolved = [];

        foreach ($psr4Paths as $namespace => $dirs) {
            foreach ((array) $dirs as $dir) {
                $absolute = base_path(rtrim($dir, '/'));

                if (!is_dir($absolute)) {
                    $this->line("  <fg=yellow>⚠</> Skipping missing PSR-4 path: {$dir} ({$namespace})");
                    continue;
                }

                $resolved[rtrim($namespace, '\\') . ' → ' . $dir] = $absolute;
            }
        }

        foreach ($classmapPaths as $dir) {
            $absolute = base_path(rtrim($dir, '/'));
            if (is_dir($absolute)) {
                $resolved['classmap:' . $dir] = $absolute;
            }
        }

        return $resolved;
    }

    private function resolveRelativePath(string $path): string
    {
        return str_starts_with($path, '/') ? $path : base_path($path);
    }

    // ─── Scanning ──────────────────────────────────────────────────────────────

    /** @return CodeChunk[] */
    private function scanPath(CodeChunker $chunker, string $absolutePath, string $label): array
    {
        $this->line("  <fg=cyan>→</> Scanning: <fg=white>{$label}</>");

        if (is_file($absolutePath)) {
            return $this->chunkSingleFile($chunker, $absolutePath);
        }

        $phpFiles = $this->collectPhpFiles($absolutePath);

        if (empty($phpFiles)) {
            $this->line("    <fg=yellow>No .php files found.</>");
            return [];
        }

        $bar = $this->output->createProgressBar(count($phpFiles));
        $bar->setFormat('    %current%/%max% [%bar%] %percent:3s%% — %message%');
        $bar->setMessage('starting...');
        $bar->start();

        $chunks = [];

        foreach ($phpFiles as $file) {
            $relative = ltrim(str_replace($absolutePath, '', $file), DIRECTORY_SEPARATOR);
            $bar->setMessage($relative);
            $chunks = array_merge($chunks, $this->chunkSingleFile($chunker, $file, $relative));
            $bar->advance();
        }

        $bar->setMessage('done');
        $bar->finish();
        $this->newLine();

        return $chunks;
    }

    /** @return CodeChunk[] */
    private function chunkSingleFile(CodeChunker $chunker, string $absolute, string $relative = ''): array
    {
        $relative = $relative ?: basename($absolute);

        try {
            $chunks = $chunker->chunkFile($absolute, $relative, $this->typeMap);
            $this->totalFiles++;
            $this->totalChunks += count($chunks);
            return $chunks;
        } catch (Throwable $e) {
            $this->skippedFiles++;
            $this->errors[] = ['file' => $relative, 'message' => $e->getMessage()];
            return [];
        }
    }

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

    // ─── Export ────────────────────────────────────────────────────────────────

    /**
     * Write all chunks to the given file path in JSON or YAML format.
     * The destination directory is created automatically if it does not exist.
     *
     * @param CodeChunk[] $chunks
     */
    private function exportChunks(
        ChunkExporter $exporter,
        array         $chunks,
        string        $exportPath,
        string        $format,
    ): bool {
        $this->newLine();
        $this->components->info("Exporting " . count($chunks) . " chunks as {$format}...");

        // Resolve export path relative to base_path() if not absolute
        $absoluteExportPath = str_starts_with($exportPath, '/')
            ? $exportPath
            : base_path($exportPath);

        // Ensure the target directory exists
        $dir = dirname($absoluteExportPath);
        if (!is_dir($dir)) {
            if (!mkdir($dir, 0755, recursive: true)) {
                $this->components->error("Could not create export directory: {$dir}");
                return false;
            }
        }

        // Check the directory is writable before doing any work
        if (!is_writable($dir)) {
            $this->components->error("Export directory is not writable: {$dir}");
            return false;
        }

        try {
            match ($format) {
                'json' => $exporter->toJsonFile($chunks, $absoluteExportPath),
                'yaml' => $exporter->toYamlFile($chunks, $absoluteExportPath),
                'text' => $exporter->toTextFile($chunks, $absoluteExportPath),
            };

            $size = $this->humanFileSize(filesize($absoluteExportPath));
            $this->components->twoColumnDetail(
                'Exported to',
                "<fg=green>{$exportPath}</> <fg=gray>({$size})</>"
            );

            return true;

        } catch (Throwable $e) {
            $this->components->error("Export failed: " . $e->getMessage());
            return false;
        }
    }

    // ─── Indexing ──────────────────────────────────────────────────────────────

    /** @param CodeChunk[] $chunks */
    private function indexChunks(VectorIndexer $indexer, array $chunks, int $batchSize): void
    {
        $this->newLine();
        $this->components->info('Indexing ' . count($chunks) . ' chunks into vector DB...');

        $bar = $this->output->createProgressBar(count($chunks));
        $bar->setFormat('  %current%/%max% [%bar%] %percent:3s%% — batch %message%');
        $bar->setMessage('1');
        $bar->start();

        $batchCount = 0;

        foreach (array_chunk($chunks, $batchSize) as $batch) {
            $batchCount++;
            $bar->setMessage((string) $batchCount);

            try {
                $indexer->indexChunks($batch);
            } catch (Throwable $e) {
                $this->newLine();
                $this->components->error("Batch {$batchCount} failed: " . $e->getMessage());
            }

            $bar->advance(count($batch));
        }

        $bar->setMessage('done');
        $bar->finish();
        $this->newLine();
    }

    // ─── Console output ────────────────────────────────────────────────────────

    /** @param CodeChunk[] $chunks */
    private function renderConsoleOutput(array $chunks, string $format): void
    {
        $this->newLine();

        match ($format) {
            'json'    => $this->renderJsonConsole($chunks),
            'summary' => $this->renderSummaryOnly($chunks),
            default   => $this->renderTable($chunks),
        };
    }

    /** @param CodeChunk[] $chunks */
    private function renderTable(array $chunks): void
    {
        if (empty($chunks)) return;

        $rows = array_map(fn(CodeChunk $c) => [
            $c->type,
            ($c->className ? "<fg=cyan>{$c->className}</>::" : '') . "<fg=white>{$c->name}</>",
            $c->file . ':' . $c->startLine,
            implode(', ', array_slice($c->modifiers, 0, 3)) ?: '—',
            count($c->calls) . ' call(s)',
            $c->returnType ?? '—',
        ], $chunks);

        $this->table(
            ['Type', 'Symbol', 'Location', 'Modifiers', 'Calls', 'Return type'],
            $rows
        );
    }

    /** @param CodeChunk[] $chunks */
    private function renderJsonConsole(array $chunks): void
    {
        $data = array_map(fn(CodeChunk $c) => $c->toMetadata(), $chunks);
        $this->line(json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES));
    }

    /** @param CodeChunk[] $chunks */
    private function renderSummaryOnly(array $chunks): void
    {
        $byType  = array_count_values(array_map(fn($c) => $c->type, $chunks));
        $byClass = [];

        foreach ($chunks as $c) {
            if ($c->className) {
                $byClass[$c->className] = ($byClass[$c->className] ?? 0) + 1;
            }
        }

        arsort($byClass);

        $this->line('  <fg=white>Chunks by type:</>');
        foreach ($byType as $type => $count) {
            $this->line("    {$type}: {$count}");
        }

        $this->newLine();
        $this->line('  <fg=white>Top classes by chunk count:</>');
        foreach (array_slice($byClass, 0, 10, true) as $class => $count) {
            $this->line("    {$class}: {$count}");
        }
    }

    private function renderScanPlan(
        array   $paths,
        bool    $isDryRun,
        bool    $noIndex,
        int     $batchSize,
        ?string $exportPath,
        ?string $exportFormat,
    ): void {
        $mode = match (true) {
            $isDryRun => '<fg=yellow>dry run</>',
            $noIndex  => '<fg=cyan>parse only</>',
            default   => '<fg=green>index</>',
        };

        $this->line("  Mode:       {$mode}");
        $this->line("  Paths:      " . count($paths));

        if ($exportPath) {
            $this->line("  Export:     <fg=white>{$exportPath}</> <fg=gray>({$exportFormat})</>");
        }

        if (!$noIndex) {
            $this->line("  Batch size: {$batchSize}");
        }

        $this->newLine();
    }

    private function renderSummary(
        bool    $noIndex,
        bool    $isDryRun,
        ?string $exportPath,
        ?string $exportFormat,
    ): void {
        $this->newLine();

        $action = match (true) {
            $isDryRun => 'Would process',
            $noIndex  => 'Parsed',
            default   => 'Indexed',
        };

        $this->components->twoColumnDetail("{$action} chunks",  "<fg=green>{$this->totalChunks}</>");
        $this->components->twoColumnDetail('Files processed',   "<fg=green>{$this->totalFiles}</>");

        if ($this->skippedFiles > 0) {
            $this->components->twoColumnDetail(
                'Files skipped (parse error)',
                "<fg=yellow>{$this->skippedFiles}</>"
            );
        }

        if (!$noIndex && !$isDryRun) {
            $this->components->twoColumnDetail('Vector DB', '<fg=green>✓ updated</>');
        }
    }

    // ─── Helpers ───────────────────────────────────────────────────────────────

    private function humanFileSize(int|false $bytes): string
    {
        if ($bytes === false) return 'unknown size';

        return match (true) {
            $bytes >= 1_048_576 => round($bytes / 1_048_576, 1) . ' MB',
            $bytes >= 1_024     => round($bytes / 1_024, 1) . ' KB',
            default             => $bytes . ' B',
        };
    }
}
