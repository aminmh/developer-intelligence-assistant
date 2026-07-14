<?php

namespace App\Console\Commands;

use App\Services\CodeParser\CodeChunker;
use Illuminate\Console\Command;

class CodeParse extends Command
{
    protected $signature = 'code:chunk
        {path : File or directory to parse}
        {--output= : Output filename (relative to storage/app)}';

    protected $description = 'Parse PHP file(s) and save chunks as JSON';

    public function handle(CodeChunker $chunker): int
    {
        $path = $this->argument('path');

        if (!file_exists($path)) {
            $this->error("Path not found: $path");
            return 1;
        }

        $chunks = is_file($path)
            ? $chunker->chunkFile($path)
            : iterator_to_array($chunker->chunkDirectory($path));

        if (empty($chunks)) {
            $this->warn('No functions or methods found.');
            return 0;
        }

        $filename = $this->option('output') ?? $this->defaultFilename($path);
        $filepath = storage_path("app/$filename");

        file_put_contents(
            $filepath,
            json_encode($chunks, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES)
        );

        $this->info(sprintf('Saved %d chunk(s) to %s', count($chunks), $filepath));
        return 0;
    }

    private function defaultFilename(string $path): string
    {
        $name = is_file($path) ? pathinfo($path, PATHINFO_FILENAME) : basename($path);
        return "code-chunks-{$name}-" . now()->format('Ymd-His') . '.json';
    }
}
