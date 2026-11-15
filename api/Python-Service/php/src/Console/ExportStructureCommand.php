<?php

declare(strict_types=1);

namespace App\Console;

/**
 * Pure-PHP replacement for the former Laravel `project:export-structure` artisan
 * command. Runs `tree` over a project directory and prints a RAG-friendly
 * Markdown blueprint to stdout.
 *
 * Usage:
 *   php bin/project-export-structure /abs/project
 *   php bin/project-export-structure /abs/project --depth=4 --include-hidden
 *
 * Exit codes: 0 = ok, 1 = failure.
 */
final class ExportStructureCommand
{
    public function run(array $argv): int
    {
        [$positional, $opts] = $this->parseArgs($argv);

        $projectPath = $positional[0] ?? null;
        if ($projectPath === null) {
            fwrite(STDERR, "Usage: php bin/project-export-structure <path> [--depth=4] [--include-hidden]\n");
            return 1;
        }

        $real = realpath($projectPath);
        if ($real === false || !is_dir($real)) {
            fwrite(STDERR, "Error: path does not exist: {$projectPath}\n");
            return 1;
        }

        $maxDepth = max(1, (int) ($opts['depth'] ?? 4));
        $includeHidden = !empty($opts['include-hidden']);

        $excludedPatterns = [
            'vendor', 'node_modules', '.git', '.idea', '.vscode', '.DS_Store',
            'storage/framework', 'storage/logs', 'bootstrap/cache',
            'public/build', 'public/hot', 'var/cache', 'var/log',
            'coverage', 'dist', 'build', '*.log', '*.lock',
        ];

        $command = ['tree', '-F', '-I', implode('|', $excludedPatterns), '-L', (string) $maxDepth];
        if ($includeHidden) {
            $command[] = '-a';
        }
        $command[] = $real;

        [$exit, $stdout, $stderr] = $this->runProcess($command);
        if ($exit !== 0) {
            fwrite(STDERR, "Unable to run the tree command: " . trim($stderr) . "\n");
            return 1;
        }

        $treeOutput = trim($stdout);
        echo $this->buildMarkdown($real, $maxDepth, $excludedPatterns, $treeOutput);
        return 0;
    }

    /** @return array{list<string>, array<string, mixed>} */
    private function parseArgs(array $argv): array
    {
        $positional = [];
        $opts = [];
        foreach (array_slice($argv, 1) as $arg) {
            if (str_starts_with($arg, '--') && str_contains($arg, '=')) {
                [$key, $value] = explode('=', $arg, 2);
                $opts[substr($key, 2)] = $value;
            } elseif (str_starts_with($arg, '--')) {
                $opts[substr($arg, 2)] = true;
            } else {
                $positional[] = $arg;
            }
        }
        return [$positional, $opts];
    }

    /** @param string[] $cmd @return array{int, string, string} */
    private function runProcess(array $cmd): array
    {
        $descriptors = [
            0 => ['pipe', 'r'],
            1 => ['pipe', 'w'],
            2 => ['pipe', 'w'],
        ];
        $proc = proc_open($cmd, $descriptors, $pipes);
        if (!is_resource($proc)) {
            return [1, '', 'Could not start process'];
        }
        fclose($pipes[0]);
        $stdout = stream_get_contents($pipes[1]);
        $stderr = stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        $exit = proc_close($proc);
        return [$exit, $stdout, $stderr];
    }

    /**
     * @param string[] $excludedPatterns
     */
    private function buildMarkdown(
        string $projectPath,
        int $maxDepth,
        array $excludedPatterns,
        string $treeOutput,
    ): string {
        $excluded = implode(', ', array_map(
            static fn(string $pattern): string => "`{$pattern}`",
            $excludedPatterns,
        ));
        $timestamp = date(DATE_ATOM);

        return <<<MARKDOWN
# Project Structure Blueprint

## Metadata

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
