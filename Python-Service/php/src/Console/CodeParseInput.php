<?php

declare(strict_types=1);

namespace App\Console;

use App\CodeParser\WorkspaceTypeScanner;
use Throwable;

/** Resolves scan inputs and loads the optional workspace type map. */
final class CodeParseInput
{
    private function __construct(public readonly string $path)
    {
    }

    public static function resolve(string $path): ?self
    {
        $real = realpath($path);
        if ($real === false || !file_exists($real)) {
            return null;
        }

        return new self($real);
    }

    /** @return array{map: array<string, mixed>, warning: ?string} */
    public function loadTypeMap(?string $typeMapFile): array
    {
        $typeMap = [];
        if ($typeMapFile !== null && is_file($typeMapFile)) {
            $typeMap = json_decode((string) file_get_contents($typeMapFile), true) ?: [];
        }

        if (is_dir($this->path) && empty($typeMap)) {
            try {
                $typeMap = (new WorkspaceTypeScanner())->scan($this->path);
            } catch (Throwable $e) {
                return [
                    'map' => $typeMap,
                    'warning' => 'Warning: type scanner failed: ' . $e->getMessage(),
                ];
            }
        }

        return ['map' => $typeMap, 'warning' => null];
    }

    /** @return list<array{absolute: string, relative: string}> */
    public function files(): array
    {
        if (is_file($this->path)) {
            return [['absolute' => $this->path, 'relative' => basename($this->path)]];
        }

        $iterator = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($this->path, \FilesystemIterator::SKIP_DOTS)
        );
        $files = [];
        foreach ($iterator as $file) {
            if ($file->getExtension() !== 'php') {
                continue;
            }
            $absolute = $file->getPathname();
            $files[] = [
                'absolute' => $absolute,
                'relative' => ltrim(str_replace($this->path, '', $absolute), DIRECTORY_SEPARATOR),
            ];
        }

        usort($files, static fn(array $left, array $right): int => $left['absolute'] <=> $right['absolute']);
        return $files;
    }
}
