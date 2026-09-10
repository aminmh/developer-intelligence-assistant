<?php

declare(strict_types=1);

namespace App\Inspection;

use App\CodeParser\PhpAstParser;

/**
 * Compatibility facade for the project-wide symbol inspection CLI.
 *
 * Discovery, definitions, and references live in dedicated collaborators;
 * this class preserves the original public API used by InspectCommand.
 */
final class SymbolExporter
{
    private PhpFileFinder $files;
    private SymbolDefinitionExporter $definitions;
    private SymbolReferenceExporter $references;

    public function __construct(?PhpAstParser $astParser = null)
    {
        $parser = $astParser ?? new PhpAstParser();
        $this->files = new PhpFileFinder();
        $this->definitions = new SymbolDefinitionExporter($parser);
        $this->references = new SymbolReferenceExporter($parser);
    }

    /** @param list<string> $exclude */
    public function phpFiles(string $rootPath, array $exclude = []): array
    {
        return $this->files->find($rootPath, $exclude);
    }

    /** @param list<string> $exclude */
    public function exportDefinitions(string $rootPath, array $exclude = []): array
    {
        return $this->definitions->export($this->phpFiles($rootPath, $exclude));
    }

    /** @param list<string> $exclude */
    public function exportReferences(string $rootPath, array $exclude = []): array
    {
        return $this->references->export($this->phpFiles($rootPath, $exclude));
    }

    /**
     * Export the complete project symbol index in one CLI invocation.
     *
     * The definitions and references remain separate streams so callers can
     * apply different resolution/retention policies, while sharing discovery
     * and the command boundary used by the indexing pipeline.
     *
     * @param list<string> $exclude
     * @return array{definitions: list<array<string,mixed>>, references: list<array<string,mixed>>}
     */
    public function exportIndex(string $rootPath, array $exclude = []): array
    {
        $files = $this->phpFiles($rootPath, $exclude);

        return [
            'definitions' => $this->definitions->export($files),
            'references' => $this->references->export($files),
        ];
    }
}
