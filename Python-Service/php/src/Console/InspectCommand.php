<?php

declare(strict_types=1);

namespace App\Console;

use App\Inspection\AstProbe;
use App\Inspection\SkeletonExporter;
use App\Inspection\SymbolExporter;

/**
 * Deterministic multi-tier PHP inspection CLI (JSON on stdout, errors on stderr).
 *
 * Usage:
 *   php bin/project-inspect skeleton <file>
 *   php bin/project-inspect attributes <file> [class]
 *   php bin/project-inspect method <file> <method>
 *   php bin/project-inspect symbols <dir> [exclude_csv]
 *   php bin/project-inspect references <dir> [exclude_csv]
 *
 * exclude_csv: comma-separated relative dir segments to skip (e.g.
 * tests,coverage). Built-ins (vendor, node_modules, .git, …) always skip.
 *
 * Exit codes: 0 = ok (stdout carries one JSON document), 1 = failure (stderr
 * explains), 2 = usage error.
 */
final class InspectCommand
{
    private const JSON_FLAGS = JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE;

    /** @return list<string> */
    private function excludes(array $argv): array
    {
        $csv = $argv[3] ?? '';
        if ($csv === '') {
            return [];
        }
        return array_values(array_filter(array_map('trim', explode(',', $csv)), fn ($s) => $s !== ''));
    }

    public function run(array $argv): int
    {
        $sub = $argv[1] ?? null;
        $path = $argv[2] ?? null;

        try {
            return match ($sub) {
                'skeleton' => $this->filePath($path)
                    ? $this->emit((new SkeletonExporter())->skeletonFile(realpath($path)))
                    : $this->usage("skeleton <file>"),
                'attributes' => $this->filePath($path)
                    ? $this->emit(
                        (new AstProbe())->attributes(realpath($path), isset($argv[3]) && $argv[3] !== '' ? $argv[3] : null)
                    )
                    : $this->usage("attributes <file> [class]"),
                'method' => $this->filePath($path) && isset($argv[3])
                    ? $this->emitMethod((new AstProbe()), realpath($path), $argv[3])
                    : $this->usage("method <file> <method>"),
                'symbols' => $this->dirPath($path)
                    ? $this->emit((new SymbolExporter())->exportDefinitions(realpath($path), $this->excludes($argv)))
                    : $this->usage("symbols <dir> [exclude_csv]"),
                'references' => $this->dirPath($path)
                    ? $this->emit((new SymbolExporter())->exportReferences(realpath($path), $this->excludes($argv)))
                    : $this->usage("references <dir> [exclude_csv]"),
                default => $this->usage('<skeleton|attributes|method|symbols|references> …'),
            };
        } catch (\Throwable $e) {
            fwrite(STDERR, 'Error: ' . $e->getMessage() . "\n");
            return 1;
        }
    }

    private function emit(array $payload): int
    {
        echo json_encode($payload, self::JSON_FLAGS), "\n";
        return 0;
    }

    private function emitMethod(AstProbe $probe, string $file, string $method): int
    {
        $found = $probe->method($file, $method);
        if ($found === null) {
            fwrite(STDERR, "Error: method '$method' not found in $file\n");
            return 1;
        }
        return $this->emit($found);
    }

    private function filePath(?string $path): bool
    {
        return $path !== null && is_file($path);
    }

    private function dirPath(?string $path): bool
    {
        return $path !== null && is_dir($path);
    }

    private function usage(string $expected): int
    {
        fwrite(STDERR, "Usage: php bin/project-inspect $expected\n");
        return 2;
    }
}
