<?php

namespace App\CodeParser\Models;

/** A source-oriented, AST-bounded retrieval chunk. */
final readonly class CodeChunk
{
    public const SCHEMA_VERSION = 'ast-chunk-v1';

    public function __construct(
        public string $code, public string $type, public string $name,
        public ?string $className, public ?string $classFqn, public ?string $namespace,
        public string $file, public int $startLine, public int $endLine,
        public array $parameters, public ?string $returnType, public array $calls,
        public ?string $docblock, public array $modifiers,
        public string $chunkId = '', public string $language = 'php', public string $nodeType = '',
        public string $symbolName = '', public string $fqSymbolName = '', public string $signature = '',
        public ?string $parentSymbol = null, public ?string $methodName = null,
        public ?string $functionName = null, public ?string $visibility = null, public bool $isStatic = false,
        public array $imports = [], public array $extends = [], public array $implements = [], public array $traits = [],
        public array $referencedSymbols = [], public array $literals = [], public array $annotations = [],
        public array $frameworkHints = [], public array $domainTags = [], public bool $lowInformation = false,
        public ?string $summary = null, public string $schemaVersion = self::SCHEMA_VERSION,
    ) {}

    /** Flat normalized metadata, with legacy aliases retained for compatibility. */
    public function toMetadata(): array
    {
        $chunkId = $this->chunkId !== '' ? $this->chunkId : $this->deterministicId();
        $nodeType = $this->nodeType !== '' ? $this->nodeType : $this->type;
        $symbolName = $this->symbolName !== '' ? $this->symbolName : $this->name;
        $fqSymbolName = $this->fqSymbolName !== '' ? $this->fqSymbolName
            : ($this->classFqn ? $this->classFqn . ($this->name !== '' ? '::' . $this->name : '') : $this->name);
        return [
            'schema_version' => $this->schemaVersion, 'chunk_id' => $chunkId, 'file_path' => $this->file,
            'language' => $this->language, 'node_type' => $nodeType, 'symbol_name' => $symbolName,
            'fq_symbol_name' => $fqSymbolName, 'namespace' => $this->namespace, 'class_name' => $this->className,
            'method_name' => $this->methodName, 'function_name' => $this->functionName, 'signature' => $this->signature,
            'parent_symbol' => $this->parentSymbol, 'visibility' => $this->visibility, 'is_static' => $this->isStatic,
            'imports' => $this->imports, 'extends' => $this->extends, 'implements' => $this->implements,
            'traits' => $this->traits, 'docstring' => $this->docblock, 'start_line' => $this->startLine,
            'end_line' => $this->endLine, 'loc' => max(0, $this->endLine - $this->startLine + 1),
            'called_symbols' => $this->flattenCalls(), 'referenced_symbols' => $this->referencedSymbols,
            'literals' => $this->literals, 'annotations' => $this->annotations,
            'framework_hints' => $this->frameworkHints, 'domain_tags' => $this->domainTags,
            'low_information' => $this->lowInformation, 'summary' => $this->summary, 'code' => $this->code,
            'type' => $this->type, 'name' => $this->name, 'class' => $this->className, 'class_fqn' => $this->classFqn,
            'modifiers' => $this->modifiers, 'parameters' => $this->parameters, 'return_type' => $this->returnType,
            'calls' => $this->flattenCalls(), 'callers' => [], 'has_docblock' => $this->docblock !== null,
        ];
    }

    /** @return string[] */
    public function flattenCalls(): array
    {
        $flat = [];
        foreach ($this->calls as $owner => $methods) foreach ($methods as $method) $flat[] = "{$owner}::{$method}";
        return array_values(array_unique($flat));
    }

    public function toEmbeddableText(): string
    {
        $header = ["File: {$this->file}"];
        if ($this->namespace) $header[] = "Namespace: {$this->namespace}";
        if ($this->className) $header[] = "Class: {$this->className}";
        if ($this->methodName) $header[] = "Method: {$this->methodName}";
        if ($this->functionName && !$this->methodName) $header[] = "Function: {$this->functionName}";
        if ($this->signature !== '') $header[] = "Signature: {$this->signature}";
        if ($this->docblock) $header[] = $this->docblock;
        if ($this->summary) $header[] = "Summary: {$this->summary}";
        if ($calls = $this->flattenCalls()) $header[] = 'Calls: ' . implode(', ', $calls);
        if ($this->referencedSymbols) $header[] = 'References: ' . implode(', ', $this->referencedSymbols);
        return implode("\n", $header) . "\n\n" . $this->code;
    }

    /** @return string[] */
    public function flatCallNames(): array
    {
        if (empty($this->calls)) return [];
        return array_values(array_unique(array_merge(...array_values($this->calls))));
    }

    /** @return string[] */
    public function flatCallClasses(): array
    {
        return array_values(array_filter(array_keys($this->calls), fn(string $key) => !str_starts_with($key, '__')));
    }

    private function deterministicId(): string
    {
        return sha1(implode('|', [$this->file, $this->fqSymbolName, $this->type, $this->startLine, $this->endLine]));
    }
}
