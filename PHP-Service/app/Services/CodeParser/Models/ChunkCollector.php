<?php

namespace App\Services\CodeParser\Models;

use PhpParser\Node;
use PhpParser\Node\Stmt\Class_;
use PhpParser\Node\Stmt\ClassMethod;
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
     * Constructor property-injection map for the current class.
     * Built when we enter a class node, reset when we leave.
     *
     * Maps variable name (without $) → FQCN
     * e.g. "paymentService" => "App\\Services\\PaymentService"
     *
     * @var array<string, string>
     */
    private array $constructorTypes = [];

    public array $chunks = [];

    public function __construct(
        private readonly string        $sourceCode,
        private readonly string        $filePath,
        private readonly PrettyPrinter $printer,
    ) {}

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

        if ($node instanceof Class_ || $node instanceof Interface_ || $node instanceof Trait_) {
            $className = $node->name?->toString() ?? '(anonymous)';
            $this->classStack[] = $className;

            // Extract typed constructor parameters for this class so that
            // $this->foo type hints can be resolved inside every method body.
            if ($node instanceof Class_) {
                $this->constructorTypes = $this->extractConstructorTypes($node);
            }
        }
    }

    public function leaveNode(Node $node): void
    {
        if ($node instanceof Class_ || $node instanceof Interface_ || $node instanceof Trait_) {
            array_pop($this->classStack);
            $this->constructorTypes = [];
            return;
        }

        if ($node instanceof Function_ || $node instanceof ClassMethod) {
            $this->chunks[] = $this->buildChunk($node);
        }
    }

    // ─── Chunk construction ────────────────────────────────────────────────────

    private function buildChunk(Function_|ClassMethod $node): CodeChunk
    {
        $className = $this->classStack ? end($this->classStack) : null;
        $type      = $node instanceof ClassMethod ? 'method' : 'function';
        $name      = $node->name->toString();

        $start = $node->getStartLine();
        $end   = $node->getEndLine();
        $lines = explode("\n", $this->sourceCode);
        $code  = implode("\n", array_slice($lines, $start - 1, $end - $start + 1));

        return new CodeChunk(
            code:        $code,
            type:        $type,
            name:        $name,
            className:   $className,
            file:        $this->filePath,
            startLine:   $start,
            endLine:     $end,
            parameters:  $this->extractParams($node),
            returnType:  $this->extractReturnType($node),
            calls:       $this->extractCalls($node, $className),
            docblock:    $this->extractDocblock($node),
            modifiers:   $this->extractModifiers($node),
        );
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
            new class($calls, $localVarTypes, $currentFqcn, $this->useMap) extends NodeVisitorAbstract
            {
                public function __construct(
                    private array  &$calls,
                    private array  $varTypes,     // varName → FQCN
                    private ?string $currentFqcn,
                    private array  $useMap,        // alias → FQCN
                ) {}

                public function enterNode(Node $node): void
                {
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
