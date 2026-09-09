<?php

declare(strict_types=1);

namespace App\Console;

use App\CodeParser\CodeChunker;
use Throwable;

/** Runs the chunker over the files selected by CodeParseInput. */
final class CodeParseScanner
{
    public function __construct(private readonly CodeChunker $chunker)
    {
    }

    /** @return array{chunks: list<\App\CodeParser\Models\CodeChunk>, report: CodeParseReport} */
    public function scan(CodeParseInput $input, array $typeMap): array
    {
        $chunks = [];
        $report = new CodeParseReport();

        foreach ($input->files() as $file) {
            try {
                $fileChunks = $this->chunker->chunkFile($file['absolute'], $file['relative'], $typeMap);
                $report->recordSuccess($fileChunks);
                $chunks = array_merge($chunks, $fileChunks);
            } catch (Throwable $e) {
                $report->recordFailure($file['relative'] ?: basename($file['absolute']), $e->getMessage());
            }
        }

        return ['chunks' => $chunks, 'report' => $report];
    }
}
