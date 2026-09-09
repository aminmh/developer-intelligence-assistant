<?php

declare(strict_types=1);

namespace App\Inspection;

/**
 * Finds PHP source files for workspace inspection.
 *
 * Keeping discovery separate from AST traversal makes the exporters easier to
 * test and keeps exclusion policy in one place.
 */
final class PhpFileFinder
{
    private const SKIP_DIRS = [
        'vendor', 'node_modules', '.git', '__MACOSX', '.idea', '.vscode',
        'dist', 'build', 'coverage', '.venv', 'var/cache', 'bootstrap/cache',
    ];

    /** @param list<string> $exclude extra relative dir segments to skip */
    public function find(string $rootPath, array $exclude = []): array
    {
        $skip = array_merge(self::SKIP_DIRS, $exclude);
        $out = [];
        $iterator = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($rootPath, \FilesystemIterator::SKIP_DOTS)
        );
        foreach ($iterator as $file) {
            if (!$file->isFile() || $file->getExtension() !== 'php') {
                continue;
            }
            $path = $file->getPathname();
            $skipped = false;
            foreach ($skip as $dir) {
                if ($dir !== ''
                    && str_contains($path, DIRECTORY_SEPARATOR . $dir . DIRECTORY_SEPARATOR)
                ) {
                    $skipped = true;
                    break;
                }
            }
            if (!$skipped) {
                $out[] = $path;
            }
        }
        sort($out);
        return $out;
    }
}
