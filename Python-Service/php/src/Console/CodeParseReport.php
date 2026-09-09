<?php

declare(strict_types=1);

namespace App\Console;

/** Aggregates scan results without coupling renderers to the scanner. */
final class CodeParseReport
{
    /** @var list<array{file: string, message: string}> */
    private array $errors = [];

    private int $totalChunks = 0;
    private int $totalFiles = 0;
    private int $skippedFiles = 0;

    /** @param array<mixed> $chunks */
    public function recordSuccess(array $chunks): void
    {
        $this->totalFiles++;
        $this->totalChunks += count($chunks);
    }

    public function recordFailure(string $file, string $message): void
    {
        $this->skippedFiles++;
        $this->errors[] = ['file' => $file, 'message' => $message];
    }

    /** @return list<array{file: string, message: string}> */
    public function errors(): array
    {
        return $this->errors;
    }

    public function hasErrors(): bool
    {
        return $this->errors !== [];
    }

    public function totalFiles(): int
    {
        return $this->totalFiles;
    }

    public function totalChunks(): int
    {
        return $this->totalChunks;
    }

    public function skippedFiles(): int
    {
        return $this->skippedFiles;
    }
}
