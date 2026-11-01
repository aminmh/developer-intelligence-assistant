<?php

declare(strict_types=1);

namespace App\Console;

use App\CodeParser\Models\CodeChunk;

/** Formats code-parse results for the CLI's supported output modes. */
final class CodeParseRenderer
{
    /** @param CodeChunk[] $chunks */
    public function render(string $output, array $chunks): string
    {
        return match ($output) {
            'json' => $this->json($chunks),
            'text' => $this->text($chunks),
            'summary' => $this->summary($chunks),
            'none' => '',
            default => '',
        };
    }

    /** @param CodeChunk[] $chunks */
    private function json(array $chunks): string
    {
        $data = array_map(static fn(CodeChunk $chunk): array => $chunk->toMetadata(), $chunks);
        return json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR) . "\n";
    }

    /** @param CodeChunk[] $chunks */
    private function text(array $chunks): string
    {
        $rendered = '';
        $total = count($chunks);
        foreach ($chunks as $index => $chunk) {
            $position = $index + 1;
            $label = $chunk->className
                ? "{$chunk->className}::{$chunk->name}()"
                : "{$chunk->name}()";
            $location = "{$chunk->file}:{$chunk->startLine}";
            $separator = str_repeat('=', 64);
            $rendered .= $separator . "\n";
            $rendered .= "[{$position}/{$total}] {$label}  •  {$location}\n";
            $rendered .= $separator . "\n";
            $rendered .= $chunk->toEmbeddableText() . "\n\n";
        }
        return $rendered;
    }

    /** @param CodeChunk[] $chunks */
    private function summary(array $chunks): string
    {
        $byType = array_count_values(array_map(static fn(CodeChunk $chunk): string => $chunk->type, $chunks));
        $byClass = [];
        foreach ($chunks as $chunk) {
            if ($chunk->className) {
                $byClass[$chunk->className] = ($byClass[$chunk->className] ?? 0) + 1;
            }
        }
        arsort($byClass);

        $rendered = "Chunks by type:\n";
        foreach ($byType as $type => $count) {
            $rendered .= "  {$type}: {$count}\n";
        }
        $rendered .= "\nTop classes by chunk count:\n";
        foreach (array_slice($byClass, 0, 10, true) as $class => $count) {
            $rendered .= "  {$class}: {$count}\n";
        }
        return $rendered;
    }
}
