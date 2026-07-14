<?php

namespace App\Services\CodeParser\Models;

final readonly class CodeChunk
{
    public function __construct(
        public string  $code,          // exact source text of the node
        public string  $type,          // 'function' | 'method' | 'closure'
        public string  $name,          // function or method name
        public ?string $className,     // null for free functions
        public string  $file,          // relative path
        public int     $startLine,
        public int     $endLine,
        public array   $parameters,    // ['name' => '$x', 'type' => 'int|null', 'default' => ...]
        public ?string $returnType,
        /**
         * Calls made inside this chunk, keyed by the resolved FQCN of the receiver.
         *
         * Shape: array<string, string[]>
         *
         * Examples:
         *   $this->save()           → ['App\\Models\\Invoice'         => ['save']]
         *   $foo->charge()          → ['App\\Services\\PaymentService' => ['charge']]
         *   self::format()          → ['App\\Helpers\\Formatter'       => ['format']]
         *   PaymentGateway::ping()  → ['App\\External\\PaymentGateway' => ['ping']]
         *   helper()                → ['__function'                    => ['helper']]
         *   $unknown->doSomething() → ['__unknown'                     => ['doSomething']]
         */
        public array   $calls,
        public ?string $docblock,      // raw /** ... */ if present
        public array   $modifiers,     // ['public', 'static', ...]
    ) {}

    /** Flat array ready for your vector DB metadata field */
    public function toMetadata(): array
    {
        return [
            'type'           => $this->type,
            'name'           => $this->name,
            'class'          => $this->className,
            'file'           => $this->file,
            'start_line'     => $this->startLine,
            'end_line'       => $this->endLine,
            'parameters'     => $this->parameters,
            'return_type'    => $this->returnType,
            // Full keyed map: { "App\\Services\\Foo": ["bar", "baz"] }
            'calls'          => $this->calls,
            // Flat list of every method name — for Qdrant keyword filtering
            'called_methods' => $this->flatCallNames(),
            // Flat list of every resolved receiver FQCN — for dependency queries
            'called_classes' => $this->flatCallClasses(),
            'modifiers'      => $this->modifiers,
            'has_docblock'   => $this->docblock !== null,
            'docblock'       => $this->docblock,
        ];
    }

    /** Text that will be embedded — code + structured header for richer semantics */
    public function toEmbeddableText(): string
    {
        $callLines = [];
        foreach ($this->calls as $class => $methods) {
            $callLines[] = "{$class}->[" . implode(', ', $methods) . "]";
        }

        $header = implode("\n", array_filter([
            $this->docblock,
            $this->className ? "Class: {$this->className}" : null,
            "Signature: " . $this->buildSignature(),
            $callLines ? "Calls: " . implode(' | ', $callLines) : null,
        ]));

        return $header . "\n\n" . $this->code;
    }

    /**
     * All method names called — flattened across all receivers.
     * Used for simple Qdrant keyword filtering without unpacking the nested map.
     *
     * @return string[]
     */
    public function flatCallNames(): array
    {
        if (empty($this->calls)) return [];
        return array_values(array_unique(array_merge(...array_values($this->calls))));
    }

    /**
     * All resolved receiver FQCNs — excluding pseudo-keys (__function, __unknown).
     * Used for "which chunks depend on App\Services\PaymentService?" queries.
     *
     * @return string[]
     */
    public function flatCallClasses(): array
    {
        return array_values(array_filter(
            array_keys($this->calls),
            fn(string $k) => !str_starts_with($k, '__')
        ));
    }

    private function buildSignature(): string
    {
        $params = array_map(
            fn($p) => ($p['type'] ? $p['type'] . ' ' : '') . $p['name'],
            $this->parameters
        );
        $ret = $this->returnType ? ': ' . $this->returnType : '';
        return "function {$this->name}(" . implode(', ', $params) . "){$ret}";
    }
}
