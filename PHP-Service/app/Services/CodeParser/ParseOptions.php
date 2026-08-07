<?php

declare(strict_types=1);

namespace App\Services\CodeParser;

final class ParseOptions
{
    public function __construct(
        public readonly ?string $path = null,
        public readonly bool $project = false,
        public readonly ?string $exportPath = null,
        public readonly ?string $exportFormat = null,
        public readonly bool $noIndex = false,
        public readonly bool $dryRun = false,
        public readonly int $batch = 20,
    ) {}
}

