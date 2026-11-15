<?php

namespace App\CodeParser\Models;

use PhpParser\Node;
use PhpParser\Node\Stmt\Class_;
use PhpParser\Node\Stmt\ClassMethod;
use PhpParser\Node\Stmt\Enum_;
use PhpParser\Node\Stmt\Function_;
use PhpParser\Node\Stmt\Interface_;
use PhpParser\Node\Stmt\Trait_;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitorAbstract;
use PhpParser\PrettyPrinter\Standard as PrettyPrinter;

final class ChunkCollector extends NodeVisitorAbstract
{
    /** @var string[] class/trait/interface name stack */
    private array $classStack = [];

    /**
     * Use-statement map built at file level before any method is visited.
     * Maps short alias → fully qualified class name.
     *
     * e.g. "PaymentService" => "App\\Services\\PaymentService"
     *      "Foo"            => "Vendor\\Package\\Foo"
     *
     * @var array<string, string>
     */
    private array $useMap = [];

    /**
     * The namespace declared at the top of the current file.
     * e.g. "App\\Services" for `namespace App\Services;`
     * Remains null for files without a namespace declaration.
     */
    private ?string $currentNamespace = null;

    /**
     * Property type map for the current class.
     * Built when entering a class, cleared when leaving.
     *
     * Maps property name → FQCN
     * e.g. "age" → "App\\Models\\SkiSchool\\Age"
     *
     * Sources: typed property declarations, @var docblock annotations
     *
     * @var array<string, string>
     */
    private array $propertyTypes = [];

    public array $chunks = [];

    /**
     * External type map from WorkspaceTypeScanner.
     * Keys are FQCNs, values are {properties: {name→FQCN}, methods: {name→returnType}}.
     *
     * @var array<string, array{properties: array<string,string>, methods: array<string,string|null>}>
     */
    private array $externalTypeMap = [];

    private array $constructorTypes = [];

    public function __construct(
        private readonly string        $sourceCode,
        private readonly string        $filePath,
        private readonly PrettyPrinter $printer,
        array                          $typeMap = [],
    ) {
        $this->externalTypeMap = $typeMap;
    }

    // ─── Node enter / leave ────────────────────────────────────────────────────

    public function enterNode(Node $node): void
    {
        // Capture the file-level namespace — used to build the FQCN of the
        // current class without relying on use-statements (the class defined
        // in this file is never in $useMap because it is never imported).
        if ($node instanceof Node\Stmt\Namespace_) {
            $this->currentNamespace = $node->name?->toString();
        }

        // Collect use statements at file scope for FQCN resolution
        if ($node instanceof Node\Stmt\Use_) {
            foreach ($node->uses as $use) {
                $fqcn  = $use->name->toString();
                $alias = $use->alias?->toString() ?? $use->name->getLast();
                $this->useMap[$alias] = $fqcn;
            }
        }

        if ($node instanceof Node\Stmt\GroupUse) {
            $prefix = $node->prefix->toString();
            foreach ($node->uses as $use) {
                $fqcn  = $prefix . '\\' . $use->name->toString();
                $alias = $use->alias?->toString() ?? $use->name->getLast();
                $this->useMap[$alias] = $fqcn;
            }
        }

        if ($node instanceof Class_ || $node instanceof Interface_ || $node instanceof Trait_ || $node instanceof Enum_) {
            $className = $node->name?->toString() ?? '(anonymous)';
            $this->classStack[] = $className;

            if ($node instanceof Class_) {
                $this->constructorTypes = $this->extractConstructorTypes($node);
                $this->propertyTypes   = $this->extractPropertyTypes($node);

                // Merge external type map for this class (scanned before parsing)
                $fqcn = $this->currentNamespace
                    ? $this->currentNamespace . '\\' . $className
                    : $className;
                if (isset($this->externalTypeMap[$fqcn]['properties'])) {
                    foreach ($this->externalTypeMap[$fqcn]['properties'] as $propName => $propFqcn) {
                        $this->propertyTypes[$propName] = $propFqcn;
                    }
                }
            }
        }
    }

    public function leaveNode(Node $node): void
    {
        if ($node instanceof Node\Expr\Closure || $node instanceof Node\Expr\ArrowFunction) {
            $this->chunks[] = $this->buildClosureChunk($node);
            return;
        }
        if ($node instanceof Node\Stmt\Property || $node instanceof Node\Stmt\ClassConst || $node instanceof Node\Stmt\Const_) {
            $this->chunks[] = $this->buildMemberDeclarationChunk($node);
            return;
        }
        if ($node instanceof Class_ || $node instanceof Interface_ || $node instanceof Trait_ || $node instanceof Enum_) {
            $this->chunks[] = $this->buildDeclarationChunk($node);
            array_pop($this->classStack);
            $this->constructorTypes = [];
            $this->propertyTypes   = [];
            return;
        }

        if ($node instanceof Function_ || $node instanceof ClassMethod) {
            $this->chunks[] = $this->buildChunk($node);
        }
    }

    // ─── Chunk construction ────────────────────────────────────────────────────

    private function buildClosureChunk(Node\Expr\Closure|Node\Expr\ArrowFunction $node): CodeChunk
    {
        $start = $node->getStartLine();
        $end = $node->getEndLine();
        $className = $this->classStack ? end($this->classStack) : null;
        $classFqn = $className ? ($this->currentNamespace ? $this->currentNamespace . '\\' : '') . $className : null;
        $kind = $node instanceof Node\Expr\ArrowFunction ? 'arrow_function' : 'closure';
        $symbol = 'closure@' . $this->filePath . ':' . $start;
        return new CodeChunk(
            code: $this->sourceSlice($start, $end), type: $kind, name: '{closure}',
            className: $className, classFqn: $classFqn, namespace: $this->currentNamespace,
            file: $this->filePath, startLine: $start, endLine: $end, parameters: [], returnType: null,
            calls: [], docblock: $this->extractNodeDocblock($node), modifiers: [],
            nodeType: $kind, symbolName: '{closure}', fqSymbolName: $symbol, parentSymbol: $classFqn,
        );
    }

    private function buildChunk(Function_|ClassMethod $node): CodeChunk
    {
        $className = $this->classStack ? end($this->classStack) : null;
        $type      = $node instanceof ClassMethod ? 'method' : 'function';
        $name      = $node->name->toString();

        // Compute fully-qualified class name from namespace + class
        $classFqn = null;
        if ($className && $this->currentNamespace) {
            $classFqn = $this->currentNamespace . '\\' . $className;
        } elseif ($className) {
            $classFqn = $className;
        }

        $start = $node->getStartLine();
        $end   = $node->getEndLine();
        $lines = explode("\n", $this->sourceCode);
        $code  = implode("\n", array_slice($lines, $start - 1, $end - $start + 1));
        $modifiers = $this->extractModifiers($node);
        $symbol = $classFqn ? $classFqn . '::' . $name : $name;
        $signature = $this->buildSignature($name, $node);

        return new CodeChunk(
            code:        $code,
            type:        $type,
            name:        $name,
            className:   $className,
            classFqn:    $classFqn,
            namespace:   $this->currentNamespace,
            file:        $this->filePath,
            startLine:   $start,
            endLine:     $end,
            parameters:  $this->extractParams($node),
            returnType:  $this->extractReturnType($node),
            calls:       $this->extractCalls($node, $className),
            docblock:    $this->extractDocblock($node),
            modifiers:   $modifiers,
            nodeType:    $type,
            symbolName:  $name,
            fqSymbolName: $symbol,
            signature:   $signature,
            parentSymbol: $classFqn,
            methodName:  $node instanceof ClassMethod ? $name : null,
            functionName: $node instanceof Function_ ? $name : null,
            visibility:  $this->modifierValue($modifiers),
            isStatic:    in_array('static', $modifiers, true),
            imports:     array_values($this->useMap),
            referencedSymbols: $this->flattenCalls($this->extractCalls($node, $className)),
            annotations: $this->extractAnnotations($this->extractDocblock($node)),
            literals: $this->extractLiterals($node),
            frameworkHints: $this->frameworkHints(),
            lowInformation: $this->isLowInformation($name),
            metrics: ChunkMetrics::forCallable($code, $node->stmts ?? [], $node->params, fn(Node\Name $name) => $this->resolveMetricType($name)),
        );
    }

    private function buildDeclarationChunk(Class_|Interface_|Trait_|Enum_ $node): CodeChunk
    {
        $name = $node->name?->toString() ?? '(anonymous)';
        $classFqn = $this->currentNamespace ? $this->currentNamespace . '\\' . $name : $name;
        $type = $node instanceof Interface_ ? 'interface' : ($node instanceof Trait_ ? 'trait' : ($node instanceof Enum_ ? 'enum' : 'class'));
        $modifiers = [];
        if ($node instanceof Class_) {
            if ($node->isAbstract()) $modifiers[] = 'abstract';
            if ($node->isFinal()) $modifiers[] = 'final';
        }
        $start = $node->getStartLine();
        $end = $node->getEndLine();
        $code = $this->sourceSlice($start, $end);
        $extends = [];
        if ($node instanceof Class_ && $node->extends) $extends[] = $node->extends->toString();
        if ($node instanceof Interface_ && $node->extends) $extends = array_map(static fn($item) => $item->toString(), $node->extends);
        $implements = $node instanceof Class_ ? array_map(static fn($item) => $item->toString(), $node->implements) : [];
        $traits = [];
        foreach ($node->stmts ?? [] as $stmt) {
            if ($stmt instanceof Node\Stmt\TraitUse) {
                foreach ($stmt->traits as $trait) $traits[] = $trait->toString();
            }
        }
        $summary = sprintf('%s %s%s%s%s', ucfirst($type), $classFqn,
            $extends ? ' extends ' . implode(', ', $extends) : '',
            $implements ? ' implements ' . implode(', ', $implements) : '',
            $traits ? '; uses ' . implode(', ', $traits) : '');
        $methods = [];
        foreach ($node->stmts ?? [] as $stmt) {
            if ($stmt instanceof ClassMethod) $methods[] = $stmt->name->toString();
        }
        if ($methods) $summary .= '. Methods: ' . implode(', ', $methods);
        return new CodeChunk(
            code: $code, type: $type, name: $name, className: $name, classFqn: $classFqn,
            namespace: $this->currentNamespace, file: $this->filePath, startLine: $start, endLine: $end,
            parameters: [], returnType: null, calls: [], docblock: $this->extractNodeDocblock($node), modifiers: $modifiers,
            nodeType: $type, symbolName: $name, fqSymbolName: $classFqn, signature: $type . ' ' . $classFqn,
            imports: array_values($this->useMap), extends: $extends, implements: $implements, traits: $traits,
            summary: $summary, annotations: $this->extractAnnotations($this->extractNodeDocblock($node)),
            frameworkHints: $this->frameworkHints(),
            metrics: [
                'metric_schema' => ChunkMetrics::SCHEMA,
                'content_hash' => ChunkMetrics::contentHash($code),
                'constructor_dependencies' => $this->constructorDependencies($node),
            ],
        );
    }

    private function constructorDependencies(Class_|Interface_|Trait_|Enum_ $node): array
    {
        $dependencies = [];
        foreach ($node->getMethods() as $method) {
            if (strtolower($method->name->toString()) !== '__construct') continue;
            foreach ($method->params as $param) {
                if (!($param->var instanceof Node\Expr\Variable) || !is_string($param->var->name) || !$param->type) continue;
                $dependencies[] = [
                    'name' => '$' . $param->var->name,
                    'type' => $this->resolveMetricType($param->type) ?? $this->nodeTypeToString($param->type),
                ];
            }
            break;
        }
        return $dependencies;
    }

    private function resolveMetricType(Node $type): ?string
    {
        if ($type instanceof Node\NullableType) return $this->resolveMetricType($type->type);
        if ($type instanceof Node\UnionType || $type instanceof Node\IntersectionType) {
            foreach ($type->types as $part) {
                if (($resolved = $this->resolveMetricType($part)) !== null) return $resolved;
            }
            return null;
        }
        if (!$type instanceof Node\Name) return null;
        $name = $type->toString();
        if ($type instanceof Node\Name\FullyQualified) return $name;
        $parts = explode('\\', $name);
        if (isset($this->useMap[$parts[0]])) {
            $parts[0] = $this->useMap[$parts[0]];
            return implode('\\', $parts);
        }
        return $this->currentNamespace ? $this->currentNamespace . '\\' . $name : $name;
    }

    private function buildMemberDeclarationChunk(Node $node): CodeChunk
    {
        $names = [];
        if ($node instanceof Node\Stmt\Property) {
            foreach ($node->props as $property) $names[] = '$' . $property->name->toString();
            $type = 'property_group';
        } else {
            foreach ($node->consts as $constant) $names[] = $constant->name->toString();
            $type = $node instanceof Node\Stmt\ClassConst ? 'constant_group' : 'constant_group';
        }
        $name = implode(', ', $names);
        $className = $this->classStack ? end($this->classStack) : null;
        $classFqn = $className ? ($this->currentNamespace ? $this->currentNamespace . '\\' . $className : $className) : null;
        $start = $node->getStartLine();
        $end = $node->getEndLine();
        $symbol = ($classFqn ? $classFqn . '::' : '') . $name;
        $modifiers = [];
        if ($node instanceof Node\Stmt\Property || $node instanceof Node\Stmt\ClassConst) {
            $flags = $node->flags;
            if ($flags & Class_::MODIFIER_PUBLIC) $modifiers[] = 'public';
            if ($flags & Class_::MODIFIER_PROTECTED) $modifiers[] = 'protected';
            if ($flags & Class_::MODIFIER_PRIVATE) $modifiers[] = 'private';
            if ($flags & Class_::MODIFIER_STATIC) $modifiers[] = 'static';
            if ($flags & Class_::MODIFIER_FINAL) $modifiers[] = 'final';
        }
        $docblock = $this->extractNodeDocblock($node);
        return new CodeChunk(
            code: $this->sourceSlice($start, $end), type: $type, name: $name,
            className: $className, classFqn: $classFqn, namespace: $this->currentNamespace,
            file: $this->filePath, startLine: $start, endLine: $end, parameters: [], returnType: null,
            calls: [], docblock: $docblock, modifiers: $modifiers, nodeType: $type,
            symbolName: $name, fqSymbolName: $symbol, signature: $name, parentSymbol: $classFqn,
            visibility: $this->modifierValue($modifiers), isStatic: in_array('static', $modifiers, true),
            imports: array_values($this->useMap), annotations: $this->extractAnnotations($docblock),
            frameworkHints: $this->frameworkHints(), lowInformation: true,
        );
    }

    private function sourceSlice(int $start, int $end): string
    {
        $lines = explode("\n", $this->sourceCode);
        return implode("\n", array_slice($lines, max(0, $start - 1), max(0, $end - $start + 1)));
    }

    private function buildSignature(string $name, Function_|ClassMethod $node): string
    {
        $params = array_map(fn($p) => ($p['type'] ? $p['type'] . ' ' : '') . $p['name'], $this->extractParams($node));
        $ret = $this->extractReturnType($node);
        return $name . '(' . implode(', ', $params) . ')' . ($ret ? ': ' . $ret : '');
    }

    private function modifierValue(array $modifiers): ?string
    {
        foreach (['public', 'protected', 'private'] as $value) if (in_array($value, $modifiers, true)) return $value;
        return null;
    }

    private function flattenCalls(array $calls): array
    {
        $out = [];
        foreach ($calls as $owner => $methods) foreach ($methods as $method) $out[] = $owner . '::' . $method;
        return array_values(array_unique($out));
    }

    private function extractAnnotations(?string $docblock): array
    {
        if (!$docblock) return [];
        preg_match_all('/@(\w+)(?:\s+([^\n*]+))?/', $docblock, $matches, PREG_SET_ORDER);
        return array_map(static fn($match) => trim('@' . $match[1] . (isset($match[2]) ? ' ' . trim($match[2]) : '')), $matches);
    }

    private function isLowInformation(string $name): bool
    {
        return (bool) preg_match('/^(get|set|is|has)[A-Z_]/', $name);
    }

    private function extractLiterals(Node $node): array
    {
        $values = [];
        $traverser = new NodeTraverser();
        $traverser->addVisitor(new class($values) extends NodeVisitorAbstract {
            public function __construct(private array &$values) {}
            public function enterNode(Node $node): void
            {
                if ($node instanceof Node\Scalar\String_) $this->values[] = $node->value;
                elseif ($node instanceof Node\Scalar\LNumber || $node instanceof Node\Scalar\DNumber) $this->values[] = (string) $node->value;
            }
        });
        $traverser->traverse([$node]);
        return array_values(array_unique(array_slice($values, 0, 32)));
    }

    private function frameworkHints(): array
    {
        $hints = [];
        $path = strtolower($this->filePath);
        if (str_contains($path, 'laravel') || str_contains($path, 'app/http/controllers') || str_contains($path, 'routes/')) $hints[] = 'laravel';
        if (str_contains($path, 'symfony')) $hints[] = 'symfony';
        return $hints;
    }

    private function extractNodeDocblock(Node $node): ?string
    {
        foreach (array_reverse($node->getComments()) as $comment) if (str_starts_with(trim($comment->getText()), '/**')) return $comment->getText();
        return null;
    }

    // ─── Constructor type extraction ───────────────────────────────────────────

    /**
     * Walk the class's __construct parameters and map every typed property
     * injection to its resolved FQCN.
     *
     * Handles both:
     *   __construct(private PaymentService $paymentService)   ← promoted
     *   __construct(PaymentService $paymentService)           ← classic
     *
     * @return array<string, string>  varName → FQCN
     */
    private function extractConstructorTypes(Class_ $class): array
    {
        $map = [];

        foreach ($class->getMethods() as $method) {
            if ($method->name->toString() !== '__construct') {
                continue;
            }

            foreach ($method->params as $param) {
                if (!($param->var instanceof Node\Expr\Variable)) continue;
                if (!$param->type) continue;

                $varName = $param->var->name;
                $fqcn    = $this->resolveType($param->type);

                if ($fqcn !== null) {
                    $map[$varName] = $fqcn;
                }
            }

            break; // only one __construct
        }

        return $map;
    }

    /**
     * Walk the class's property declarations and extract type hints
     * from both typed properties (PHP 7.4+) and @var docblock annotations.
     *
     * @return array<string, string>  propertyName → FQCN
     */
    private function extractPropertyTypes(Class_ $class): array
    {
        $map = [];

        foreach ($class->stmts as $stmt) {
            // Typed property: public ?Age $age;
            if ($stmt instanceof Node\Stmt\Property) {
                $type = $stmt->type ?? null;
                if ($type === null) {
                    // Check @var docblock for type
                    $docComment = $stmt->getDocComment();
                    if ($docComment !== null) {
                        $type = $this->extractDocblockVarType($docComment->getText());
                    }
                }
                if ($type !== null) {
                    $fqcn = $this->resolveType($type);
                    if ($fqcn !== null) {
                        foreach ($stmt->props as $prop) {
                            $map[$prop->name->toString()] = $fqcn;
                        }
                    }
                }
            }

            // Promoted constructor property: __construct(private Age $age)
            if ($stmt instanceof ClassMethod && $stmt->name->toString() === '__construct') {
                foreach ($stmt->params as $param) {
                    if (!($param->flags & (Class_::MODIFIER_PUBLIC | Class_::MODIFIER_PROTECTED | Class_::MODIFIER_PRIVATE))) {
                        continue;
                    }
                    if (!$param->type) continue;
                    if (!($param->var instanceof Node\Expr\Variable)) continue;

                    $fqcn = $this->resolveType($param->type);
                    if ($fqcn !== null) {
                        $map[$param->var->name] = $fqcn;
                    }
                }
            }
        }

        return $map;
    }

    /**
     * Extract a type from a @var docblock annotation.
     * @return Node\Name|Node\NullableType|null
     */
    private function extractDocblockVarType(string $docblock): ?Node
    {
        if (preg_match('/@var\s+(\?)?(\w+(?:\\\\\w+)*(?:\[\])?)/', $docblock, $matches)) {
            $nullable = $matches[1] === '?';
            $typeName = $matches[2];
            $type = new Node\Name($typeName);
            return $nullable ? new Node\NullableType($type) : $type;
        }
        return null;
    }

    // ─── Call extraction ───────────────────────────────────────────────────────

    /**
     * Extract all calls inside a function/method body.
     *
     * Returns a map keyed by resolved FQCN (or pseudo-key) → list of method names.
     *
     * Receiver resolution priority:
     *   1. $this          → current class FQCN (from useMap + classStack)
     *   2. self / static  → current class FQCN
     *   3. $var           → look up in constructor injections, then method params
     *   4. ClassName::    → resolve via useMap, then as-is if already FQCN
     *   5. free function  → keyed under '__function'
     *   6. unresolvable   → keyed under '__unknown'
     *
     * @return array<string, string[]>
     */
    private function extractCalls(Function_|ClassMethod $node, ?string $currentShortClass): array
    {
        // Build a variable→FQCN map local to this method:
        // start from constructor injections, then overlay method parameters.
        $localVarTypes = $this->constructorTypes;

        foreach ($node->params as $param) {
            if (!($param->var instanceof Node\Expr\Variable)) continue;
            if (!$param->type) continue;

            $fqcn = $this->resolveType($param->type);
            if ($fqcn !== null) {
                $localVarTypes[$param->var->name] = $fqcn;
            }
        }

        // Resolve the current class to a FQCN for $this / self / static.
        //
        // The class defined in this file is NEVER in $useMap (you don't import
        // your own class). We build the FQCN directly from the namespace header
        // captured in $this->currentNamespace.
        //
        //   namespace App\Services;          → "App\Services"
        //   class PaymentService { ... }     → "App\Services\PaymentService"
        //   (no namespace declaration)       → "PaymentService"
        $currentFqcn = $currentShortClass
            ? ($this->currentNamespace
                ? $this->currentNamespace . '\\' . $currentShortClass
                : $currentShortClass)
            : null;

        $calls = [];

        $traverser = new NodeTraverser();
        $traverser->addVisitor(
            new class($calls, $localVarTypes, $currentFqcn, $this->useMap, $this->propertyTypes) extends NodeVisitorAbstract
            {
                public function __construct(
                    private array  &$calls,
                    private array  $varTypes,     // varName → FQCN
                    private ?string $currentFqcn,
                    private array  $useMap,        // alias → FQCN
                    private array  $propertyTypes, // propName → FQCN
                ) {}

                public function enterNode(Node $node): void
                {
                    // ── Track $var = $this->prop assignments for type flow ───
                    if (
                        $node instanceof Node\Expr\Assign
                        && $node->var instanceof Node\Expr\Variable
                        && is_string($node->var->name)
                        && $node->expr instanceof Node\Expr\PropertyFetch
                    ) {
                        $varName = $node->var->name;
                        $propFetch = $node->expr;

                        // $this->property
                        if (
                            $propFetch->var instanceof Node\Expr\Variable
                            && $propFetch->var->name === 'this'
                            && $propFetch->name instanceof Node\Identifier
                        ) {
                            $propName = $propFetch->name->toString();
                            if (isset($this->propertyTypes[$propName])) {
                                $this->varTypes[$varName] = $this->propertyTypes[$propName];
                            }
                        }
                    }

                    // ── Free function calls: helper() ───────────────────────
                    if (
                        $node instanceof Node\Expr\FuncCall
                        && $node->name instanceof Node\Name
                    ) {
                        $this->record('__function', $node->name->toString());
                        return;
                    }

                    // ── Instance method calls: $expr->method() ──────────────
                    if (
                        $node instanceof Node\Expr\MethodCall
                        && $node->name instanceof Node\Identifier
                    ) {
                        $receiver = $this->resolveMethodCallReceiver($node->var);
                        $this->record($receiver, $node->name->toString());
                        return;
                    }

                    // ── Static calls: ClassName::method() ───────────────────
                    if (
                        $node instanceof Node\Expr\StaticCall
                        && $node->name instanceof Node\Identifier
                    ) {
                        $receiver = $this->resolveStaticReceiver($node->class);
                        $this->record($receiver, $node->name->toString());
                        return;
                    }
                }

                /**
                 * Resolve the receiver of an instance method call.
                 *
                 * $this->foo()      → current class FQCN
                 * $service->foo()   → look up $service in varTypes
                 * $obj->foo()       → '__unknown' if no type info
                 */
                private function resolveMethodCallReceiver(Node\Expr $var): string
                {
                    // $this
                    if ($var instanceof Node\Expr\Variable && $var->name === 'this') {
                        return $this->currentFqcn ?? '__unknown';
                    }

                    // $someVar
                    if ($var instanceof Node\Expr\Variable && is_string($var->name)) {
                        return $this->varTypes[$var->name] ?? '__unknown';
                    }

                    // Chained: $this->repo->find() — resolve one level
                    if ($var instanceof Node\Expr\PropertyFetch) {
                        // We only resolve one chain level — deeper chaining falls to __unknown
                        return '__unknown';
                    }

                    return '__unknown';
                }

                /**
                 * Resolve the receiver of a static call.
                 *
                 * self::foo()   → current class FQCN
                 * static::foo() → current class FQCN
                 * Foo::foo()    → resolve Foo via useMap
                 */
                private function resolveStaticReceiver(Node\Name|Node\Expr $class): string
                {
                    if ($class instanceof Node\Name) {
                        $name = $class->toString();

                        if (in_array($name, ['self', 'static', 'parent'], true)) {
                            return $this->currentFqcn ?? '__unknown';
                        }

                        // Short class name: resolve via use statements
                        if (isset($this->useMap[$name])) {
                            return $this->useMap[$name];
                        }

                        // Already fully qualified (contains backslash)
                        if (str_contains($name, '\\')) {
                            return $name;
                        }

                        // Single unimported name — keep as-is, best effort
                        return $name;
                    }

                    return '__unknown';
                }

                /** Add a method to the calls map, deduplicating per receiver */
                private function record(string $receiver, string $method): void
                {
                    if (!isset($this->calls[$receiver])) {
                        $this->calls[$receiver] = [];
                    }

                    if (!in_array($method, $this->calls[$receiver], true)) {
                        $this->calls[$receiver][] = $method;
                    }
                }
            }
        );

        $traverser->traverse($node->stmts ?? []);

        return $calls;
    }

    // ─── Type resolution ───────────────────────────────────────────────────────

    /**
     * Resolve a type node to a FQCN string, or null if it is a scalar/built-in.
     *
     * Handles: Name, NullableType(Name), IntersectionType, UnionType.
     * Returns null for int, string, bool, float, array, callable, etc.
     */
    private function resolveType(Node $type): ?string
    {
        // Unwrap nullable: ?Foo → Foo
        if ($type instanceof Node\NullableType) {
            return $this->resolveType($type->type);
        }

        // Union / intersection: pick the first class type found
        if ($type instanceof Node\UnionType || $type instanceof Node\IntersectionType) {
            foreach ($type->types as $t) {
                $resolved = $this->resolveType($t);
                if ($resolved !== null) return $resolved;
            }
            return null;
        }

        // Identifier = built-in scalar (int, string, bool, etc.) — skip
        if ($type instanceof Node\Identifier) {
            return null;
        }

        if ($type instanceof Node\Name) {
            $name = $type->toString();

            // Built-in pseudo-types
            if (in_array($name, ['self', 'static', 'parent'], true)) {
                return $this->classStack ? end($this->classStack) : null;
            }

            // Resolve short name via use statements
            if (isset($this->useMap[$name])) {
                return $this->useMap[$name];
            }

            // Already fully qualified
            if (str_contains($name, '\\')) {
                return $name;
            }

            // Single segment — return as-is; may be in the same namespace
            return $name;
        }

        return null;
    }

    // ─── Remaining extractors ──────────────────────────────────────────────────

    private function extractParams(Function_|ClassMethod $node): array
    {
        return array_map(function (Node\Param $p) {
            return [
                'name'    => '$' . $p->var->name,
                'type'    => $p->type ? $this->nodeTypeToString($p->type) : null,
                'default' => $p->default ? $this->printer->prettyPrintExpr($p->default) : null,
            ];
        }, $node->params);
    }

    private function extractReturnType(Function_|ClassMethod $node): ?string
    {
        return $node->returnType ? $this->nodeTypeToString($node->returnType) : null;
    }

    private function nodeTypeToString(Node $type): string
    {
        return match (true) {
            $type instanceof Node\NullableType     => '?' . $this->nodeTypeToString($type->type),
            $type instanceof Node\UnionType        => implode('|', array_map([$this, 'nodeTypeToString'], $type->types)),
            $type instanceof Node\IntersectionType => implode('&', array_map([$this, 'nodeTypeToString'], $type->types)),
            $type instanceof Node\Name             => $type->toString(),
            $type instanceof Node\Identifier       => $type->name,
            default                                => (string) $type,
        };
    }

    private function extractDocblock(Function_|ClassMethod $node): ?string
    {
        foreach (array_reverse($node->getComments()) as $comment) {
            if (str_starts_with(trim($comment->getText()), '/**')) {
                return $comment->getText();
            }
        }
        return null;
    }

    private function extractModifiers(Function_|ClassMethod $node): array
    {
        if (!($node instanceof ClassMethod)) {
            return [];
        }

        $flags = $node->flags;
        $mods  = [];

        if ($flags & Node\Stmt\Class_::MODIFIER_PUBLIC)    $mods[] = 'public';
        if ($flags & Node\Stmt\Class_::MODIFIER_PROTECTED) $mods[] = 'protected';
        if ($flags & Node\Stmt\Class_::MODIFIER_PRIVATE)   $mods[] = 'private';
        if ($flags & Node\Stmt\Class_::MODIFIER_STATIC)    $mods[] = 'static';
        if ($flags & Node\Stmt\Class_::MODIFIER_ABSTRACT)  $mods[] = 'abstract';
        if ($flags & Node\Stmt\Class_::MODIFIER_FINAL)     $mods[] = 'final';

        return $mods;
    }
}
