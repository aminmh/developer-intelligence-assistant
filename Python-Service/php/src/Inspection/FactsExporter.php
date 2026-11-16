<?php

declare(strict_types=1);

namespace App\Inspection;

use App\CodeParser\PhpAstParser;
use PhpParser\Modifiers;
use PhpParser\Node;
use PhpParser\Node\Expr;
use PhpParser\Node\Scalar;
use PhpParser\Node\Stmt;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitorAbstract;
use PhpParser\PrettyPrinter\Standard as PrettyPrinter;

/**
 * Language facts for framework plugins (schema ``php-facts-1``).
 *
 * One pass over a project's PHP files with the real parser. Framework plugins
 * (Python, ``agent/plugins/frameworks``) read these facts instead of parsing
 * PHP themselves (decision: framework-knowledge-lives-in-plugins-over-language-facts).
 *
 * Per file: strict-types flag, namespace, classes (kind, extends, implements,
 * traits, attributes and docblock annotations with arguments, constants,
 * properties including constructor-promoted ones, methods), functions, calls
 * (static, function, ``new``, and method calls on ``$this``, on properties or
 * with literal arguments, each with its fluent chain, its enclosing scope and
 * the call whose closure argument contains it), and the top-level ``return``
 * value (configuration files).
 *
 * Argument values are encoded as JSON scalars, ``{"array": [...]}``,
 * ``{"class": FQN}`` for ``X::class``, ``{"const": {...}}``, ``{"call": {...}}``,
 * ``{"closure": true}`` or a bounded ``{"expr": "..."}``. Nothing is executed.
 */
final class FactsExporter
{
    public const SCHEMA = 'php-facts-1';
    public const MAX_CALLS_PER_FILE = 3000;

    private PhpAstParser $parser;
    private PrettyPrinter $printer;

    public function __construct(?PhpAstParser $parser = null)
    {
        $this->parser = $parser ?? new PhpAstParser();
        $this->printer = new PrettyPrinter();
    }

    /**
     * @param list<string> $files absolute paths
     * @return array{schema: string, files: list<array<string,mixed>>}
     */
    public function export(string $root, array $files): array
    {
        $root = rtrim($root, DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR;
        $out = [];
        foreach ($files as $path) {
            $relative = str_starts_with($path, $root) ? substr($path, strlen($root)) : $path;
            $source = @file_get_contents($path);
            if ($source === false) {
                $out[] = ['path' => $relative, 'parse_error' => ['message' => 'unreadable', 'line' => null]];
                continue;
            }
            $result = $this->parser->parse($source);
            if (!$result->isSuccessful()) {
                $out[] = ['path' => $relative,
                          'parse_error' => ['message' => $result->errorMessage, 'line' => $result->errorLine]];
                continue;
            }
            $visitor = new FactsVisitor($this->printer);
            $traverser = new NodeTraverser();
            $traverser->addVisitor($visitor);
            $traverser->traverse($result->nodes);
            $out[] = ['path' => $relative] + $visitor->facts();
        }
        return ['schema' => self::SCHEMA, 'files' => $out];
    }
}

/** @internal one file's facts */
final class FactsVisitor extends NodeVisitorAbstract
{
    private const MAX_STRING = 500;
    private const MAX_EXPR = 200;
    private const MAX_ITEMS = 100;
    private const MAX_DEPTH = 4;

    private string $namespace = '';
    private bool $strictTypes = false;
    /** @var list<array<string,mixed>> */
    private array $classes = [];
    /** @var list<int> indexes into $classes */
    private array $classStack = [];
    /** @var list<array<string,mixed>> */
    private array $functions = [];
    /** @var list<?string> */
    private array $scopeStack = [];
    /** @var list<array<string,mixed>> */
    private array $calls = [];
    private bool $callsTruncated = false;
    /** @var array<int,true> inner nodes of an already recorded call chain */
    private array $consumed = [];
    /** @var array<int,int> closure object id → id of the call it is an argument of */
    private array $closureParent = [];
    /** @var list<int> */
    private array $parentStack = [];
    /** @var array<int,true> closures that pushed onto $parentStack */
    private array $pushed = [];
    private mixed $returns = null;
    private bool $hasReturn = false;

    public function __construct(private readonly PrettyPrinter $printer)
    {
    }

    /** @return array<string,mixed> */
    public function facts(): array
    {
        return [
            'strict_types' => $this->strictTypes,
            'namespace' => $this->namespace,
            'classes' => $this->classes,
            'functions' => $this->functions,
            'calls' => $this->calls,
            'calls_truncated' => $this->callsTruncated,
        ] + ($this->hasReturn ? ['returns' => $this->returns] : []);
    }

    public function enterNode(Node $node): mixed
    {
        if ($node instanceof Stmt\Declare_) {
            foreach ($node->declares as $declare) {
                if ($declare->key->toString() === 'strict_types'
                    && $declare->value instanceof Scalar\Int_ && $declare->value->value === 1) {
                    $this->strictTypes = true;
                }
            }
        } elseif ($node instanceof Stmt\Namespace_) {
            $this->namespace = $node->name?->toString() ?? '';
        } elseif ($node instanceof Stmt\ClassLike && $node->name !== null) {
            $this->enterClass($node);
        } elseif ($node instanceof Stmt\ClassMethod && $this->classStack) {
            $this->enterMethod($node);
        } elseif ($node instanceof Stmt\Function_) {
            $fqn = $node->namespacedName?->toString() ?? $node->name->toString();
            $this->functions[] = [
                'name' => $node->name->toString(), 'fqn' => $fqn,
                'line' => $node->getStartLine(), 'end_line' => $node->getEndLine(),
                'attributes' => $this->attributes($node->attrGroups),
                'annotations' => $this->annotations($node),
            ];
            $this->scopeStack[] = $fqn;
        } elseif ($node instanceof Stmt\ClassConst && $this->classStack) {
            $class = &$this->currentClass();
            foreach ($node->consts as $const) {
                $class['constants'][] = [
                    'name' => $const->name->toString(), 'value' => $this->literal($const->value, 0),
                    'line' => $const->getStartLine(),
                ];
            }
        } elseif ($node instanceof Stmt\EnumCase && $this->classStack) {
            $class = &$this->currentClass();
            $class['constants'][] = [
                'name' => $node->name->toString(), 'case' => true,
                'value' => $node->expr === null ? null : $this->literal($node->expr, 0),
                'line' => $node->getStartLine(),
            ];
        } elseif ($node instanceof Stmt\Property && $this->classStack) {
            $class = &$this->currentClass();
            foreach ($node->props as $prop) {
                $class['properties'][] = [
                    'name' => '$' . $prop->name->toString(),
                    'default' => $prop->default === null ? null : $this->literal($prop->default, 0),
                    'type' => $node->type === null ? null : $this->printer->prettyPrint([$node->type]),
                    'static' => $node->isStatic(), 'visibility' => $this->visibility($node),
                    'attributes' => $this->attributes($node->attrGroups),
                    'line' => $prop->getStartLine(),
                ];
            }
        } elseif ($node instanceof Stmt\TraitUse && $this->classStack) {
            $class = &$this->currentClass();
            foreach ($node->traits as $trait) {
                $class['traits'][] = $trait->toString();
            }
        } elseif ($node instanceof Stmt\Return_ && !$this->classStack && !$this->scopeStack
                  && !$this->parentStack && $node->expr !== null && !$this->hasReturn) {
            $this->hasReturn = true;
            $this->returns = $this->literal($node->expr, 0);
        } elseif ($node instanceof Expr\Closure || $node instanceof Expr\ArrowFunction) {
            $id = spl_object_id($node);
            if (isset($this->closureParent[$id])) {
                $this->parentStack[] = $this->closureParent[$id];
                $this->pushed[$id] = true;
            }
        } elseif ($this->isCall($node) && !isset($this->consumed[spl_object_id($node)])) {
            $this->recordCall($node);
        }
        return null;
    }

    public function leaveNode(Node $node): mixed
    {
        if ($node instanceof Stmt\ClassLike && $node->name !== null && $this->classStack) {
            array_pop($this->classStack);
        } elseif (($node instanceof Stmt\ClassMethod && $this->classStack) || $node instanceof Stmt\Function_) {
            array_pop($this->scopeStack);
        } elseif (($node instanceof Expr\Closure || $node instanceof Expr\ArrowFunction)
                  && isset($this->pushed[spl_object_id($node)])) {
            unset($this->pushed[spl_object_id($node)]);
            array_pop($this->parentStack);
        }
        return null;
    }

    // ── classes and methods ──────────────────────────────────────────────

    private function enterClass(Stmt\ClassLike $node): void
    {
        $kind = match (true) {
            $node instanceof Stmt\Interface_ => 'interface',
            $node instanceof Stmt\Trait_ => 'trait',
            $node instanceof Stmt\Enum_ => 'enum',
            default => 'class',
        };
        $extends = [];
        $implements = [];
        if ($node instanceof Stmt\Class_) {
            $extends = $node->extends === null ? [] : [$node->extends->toString()];
            $implements = array_map(static fn($n) => $n->toString(), $node->implements);
        } elseif ($node instanceof Stmt\Interface_) {
            $extends = array_map(static fn($n) => $n->toString(), $node->extends);
        } elseif ($node instanceof Stmt\Enum_) {
            $implements = array_map(static fn($n) => $n->toString(), $node->implements);
        }
        $this->classes[] = [
            'kind' => $kind,
            'name' => $node->name->toString(),
            'fqn' => $node->namespacedName?->toString() ?? $node->name->toString(),
            'line' => $node->getStartLine(), 'end_line' => $node->getEndLine(),
            'abstract' => $node instanceof Stmt\Class_ && $node->isAbstract(),
            'final' => $node instanceof Stmt\Class_ && $node->isFinal(),
            'extends' => $extends, 'implements' => $implements, 'traits' => [],
            'attributes' => $this->attributes($node->attrGroups),
            'annotations' => $this->annotations($node),
            'constants' => [], 'properties' => [], 'methods' => [],
        ];
        $this->classStack[] = count($this->classes) - 1;
    }

    private function enterMethod(Stmt\ClassMethod $node): void
    {
        $class = &$this->currentClass();
        $params = [];
        foreach ($node->params as $param) {
            $name = $param->var instanceof Expr\Variable && is_string($param->var->name) ? '$' . $param->var->name : '$?';
            $type = $param->type === null ? null : $this->printer->prettyPrint([$param->type]);
            $promoted = $param->flags !== 0;
            $params[] = ['name' => $name, 'type' => $type, 'promoted' => $promoted,
                         'attributes' => $this->attributes($param->attrGroups)];
            if ($promoted) {
                $class['properties'][] = [
                    'name' => $name, 'default' => $param->default === null ? null : $this->literal($param->default, 0),
                    'type' => $type, 'static' => false,
                    'visibility' => ($param->flags & Modifiers::PRIVATE) ? 'private'
                        : (($param->flags & Modifiers::PROTECTED) ? 'protected' : 'public'),
                    'attributes' => $this->attributes($param->attrGroups),
                    'promoted' => true, 'line' => $param->getStartLine(),
                ];
            }
        }
        $class['methods'][] = [
            'name' => $node->name->toString(),
            // The method starts at its first attribute; name_line is the `function` line.
            'line' => $node->getStartLine(), 'name_line' => $node->name->getStartLine(), 'end_line' => $node->getEndLine(),
            'visibility' => $this->visibility($node), 'static' => $node->isStatic(),
            'abstract' => $node->isAbstract(),
            'attributes' => $this->attributes($node->attrGroups),
            'annotations' => $this->annotations($node),
            'params' => $params,
            'return_type' => $node->returnType === null ? null : $this->printer->prettyPrint([$node->returnType]),
        ];
        $this->scopeStack[] = $class['fqn'] . '::' . $node->name->toString();
    }

    /** @return array<string,mixed> */
    private function &currentClass(): array
    {
        return $this->classes[$this->classStack[count($this->classStack) - 1]];
    }

    private function visibility(Stmt\ClassMethod|Stmt\Property $node): string
    {
        return $node->isPrivate() ? 'private' : ($node->isProtected() ? 'protected' : 'public');
    }

    // ── attributes and annotations ───────────────────────────────────────

    /**
     * @param Node\AttributeGroup[] $groups
     * @return list<array<string,mixed>>
     */
    private function attributes(array $groups): array
    {
        $out = [];
        foreach ($groups as $group) {
            foreach ($group->attrs as $attr) {
                $out[] = ['name' => $attr->name->toString(), 'args' => $this->args($attr->args, 0),
                          'line' => $attr->getStartLine()];
            }
        }
        return $out;
    }

    /**
     * Doctrine-style docblock annotations: ``@Name`` or ``@Name(raw arguments)``.
     *
     * @return list<array{name: string, args: ?string, line: int}>
     */
    private function annotations(Node $node): array
    {
        $doc = $node->getDocComment();
        if ($doc === null) {
            return [];
        }
        $text = $doc->getText();
        $out = [];
        if (!preg_match_all('/@([A-Za-z_\\\\][A-Za-z0-9_\\\\]*)/', $text, $matches, PREG_OFFSET_CAPTURE)) {
            return [];
        }
        foreach ($matches[1] as [$name, $offset]) {
            $after = $offset + strlen($name);
            $args = null;
            if (($text[$after] ?? '') === '(') {
                $depth = 0;
                for ($i = $after; $i < strlen($text); $i++) {
                    $depth += $text[$i] === '(' ? 1 : ($text[$i] === ')' ? -1 : 0);
                    if ($depth === 0) {
                        $args = substr($text, $after + 1, min($i - $after - 1, self::MAX_STRING));
                        break;
                    }
                }
            }
            $out[] = ['name' => $name, 'args' => $args,
                      'line' => $doc->getStartLine() + substr_count($text, "\n", 0, $offset)];
        }
        return $out;
    }

    // ── calls ────────────────────────────────────────────────────────────

    private function isCall(Node $node): bool
    {
        return $node instanceof Expr\MethodCall || $node instanceof Expr\NullsafeMethodCall
            || $node instanceof Expr\StaticCall || $node instanceof Expr\FuncCall || $node instanceof Expr\New_;
    }

    private function recordCall(Expr $outer): void
    {
        // Unwind a fluent chain: Route::get(...)->middleware(...)->name(...).
        $chain = [];
        $root = $outer;
        while (($root instanceof Expr\MethodCall || $root instanceof Expr\NullsafeMethodCall) && $this->isCall($root->var)) {
            $chain[] = $root;
            $root = $root->var;
        }
        foreach ([$root, ...$chain] as $part) {
            $this->consumed[spl_object_id($part)] = true;
        }
        $chain = array_reverse($chain);
        if (count($this->calls) >= FactsExporter::MAX_CALLS_PER_FILE) {
            $this->callsTruncated = true;
            return;
        }
        $id = count($this->calls);
        $record = ['id' => $id, 'scope' => $this->scopeStack ? $this->scopeStack[count($this->scopeStack) - 1] : null,
                   'parent' => $this->parentStack ? $this->parentStack[count($this->parentStack) - 1] : null,
                   'line' => $root->getStartLine()];
        if ($root instanceof Expr\StaticCall) {
            $record += ['kind' => 'static', 'class' => $this->className($root->class), 'name' => $this->identifier($root->name)];
        } elseif ($root instanceof Expr\FuncCall) {
            $record += ['kind' => 'function', 'name' => $root->name instanceof Node\Name ? $root->name->toString() : null];
        } elseif ($root instanceof Expr\New_) {
            $record += ['kind' => 'new', 'class' => $this->className($root->class)];
        } else {
            $record += ['kind' => 'method', 'receiver' => $this->receiver($root->var), 'name' => $this->identifier($root->name)];
        }
        $record['args'] = $root instanceof Expr\New_ || !$root->isFirstClassCallable()
            ? $this->args($root->args ?? [], 0, $id) : [];
        $record['chain'] = array_map(fn(Expr $call) => [
            'name' => $this->identifier($call->name),
            'args' => $call->isFirstClassCallable() ? [] : $this->args($call->args, 0, $id),
            'line' => $call->getStartLine(),
        ], $chain);
        if ($this->worthKeeping($record)) {
            $this->calls[] = $record;
        }
    }

    /** @param array<string,mixed> $record */
    private function worthKeeping(array $record): bool
    {
        if ($record['kind'] !== 'method' || $record['chain']) {
            return true;
        }
        $receiver = (string) $record['receiver'];
        if ($receiver === 'this' || str_starts_with($receiver, 'prop:')) {
            return true;
        }
        foreach ($record['args'] as $arg) {
            if (!is_array($arg['value']) || !isset($arg['value']['expr'])) {
                return true;
            }
        }
        return false;
    }

    private function receiver(Expr $var): string
    {
        if ($var instanceof Expr\Variable && is_string($var->name)) {
            return $var->name === 'this' ? 'this' : 'var:' . $var->name;
        }
        if (($var instanceof Expr\PropertyFetch || $var instanceof Expr\NullsafePropertyFetch)
            && $var->var instanceof Expr\Variable && $var->var->name === 'this') {
            return 'prop:' . $this->identifier($var->name);
        }
        if ($var instanceof Expr\StaticPropertyFetch) {
            return 'static_prop:' . $this->className($var->class);
        }
        return 'expr';
    }

    /**
     * @param array<int,Node\Arg|Node\VariadicPlaceholder> $args
     * @return list<array{name: ?string, value: mixed}>
     */
    private function args(array $args, int $depth, ?int $callId = null): array
    {
        $out = [];
        foreach (array_slice($args, 0, self::MAX_ITEMS) as $arg) {
            if (!$arg instanceof Node\Arg) {
                continue;
            }
            if ($callId !== null && ($arg->value instanceof Expr\Closure || $arg->value instanceof Expr\ArrowFunction)) {
                $this->closureParent[spl_object_id($arg->value)] = $callId;
            }
            $out[] = ['name' => $arg->name?->toString(),
                      'value' => $arg->unpack ? ['expr' => '...'] : $this->literal($arg->value, $depth)];
        }
        return $out;
    }

    // ── literal encoding ────────────────────────────────────────────────

    private function literal(Expr $expr, int $depth): mixed
    {
        if ($expr instanceof Scalar\String_) {
            return mb_substr($expr->value, 0, self::MAX_STRING);
        }
        if ($expr instanceof Scalar\Int_ || $expr instanceof Scalar\Float_) {
            return $expr->value;
        }
        if ($expr instanceof Expr\UnaryMinus && ($expr->expr instanceof Scalar\Int_ || $expr->expr instanceof Scalar\Float_)) {
            return -$expr->expr->value;
        }
        if ($expr instanceof Expr\ConstFetch) {
            $name = strtolower($expr->name->toString());
            if (in_array($name, ['true', 'false', 'null'], true)) {
                return $name === 'null' ? null : $name === 'true';
            }
            return ['const' => ['class' => null, 'name' => $expr->name->toString()]];
        }
        if ($expr instanceof Expr\ClassConstFetch) {
            $class = $this->className($expr->class);
            $name = $this->identifier($expr->name);
            return $name === 'class' ? ['class' => $class] : ['const' => ['class' => $class, 'name' => $name]];
        }
        if ($expr instanceof Expr\Closure || $expr instanceof Expr\ArrowFunction) {
            return ['closure' => true];
        }
        if ($depth >= self::MAX_DEPTH) {
            return ['expr' => $this->printed($expr)];
        }
        if ($expr instanceof Expr\Array_) {
            $items = [];
            foreach (array_slice($expr->items, 0, self::MAX_ITEMS) as $item) {
                if ($item === null) {
                    continue;
                }
                $items[] = ['key' => $item->key === null ? null : $this->literal($item->key, $depth + 1),
                            'value' => $item->unpack ? ['expr' => '...'] : $this->literal($item->value, $depth + 1)];
            }
            return ['array' => $items, 'truncated' => count($expr->items) > self::MAX_ITEMS];
        }
        if ($expr instanceof Expr\BinaryOp\Concat) {
            $left = $this->literal($expr->left, $depth + 1);
            $right = $this->literal($expr->right, $depth + 1);
            if (is_string($left) && is_string($right)) {
                return mb_substr($left . $right, 0, self::MAX_STRING);
            }
            // Keep the parts (for example '/progress' . self::ID_ROUTE) so constants can be resolved later.
            $parts = [];
            foreach ([$left, $right] as $part) {
                array_push($parts, ...(is_array($part) && isset($part['concat']) ? $part['concat'] : [$part]));
            }
            return ['concat' => $parts];
        }
        if ($expr instanceof Expr\FuncCall && $expr->name instanceof Node\Name && !$expr->isFirstClassCallable()) {
            return ['call' => ['name' => $expr->name->toString(), 'args' => $this->args($expr->args, $depth + 1)]];
        }
        return ['expr' => $this->printed($expr)];
    }

    private function printed(Expr $expr): string
    {
        try {
            return mb_substr($this->printer->prettyPrintExpr($expr), 0, self::MAX_EXPR);
        } catch (\Throwable) {
            return '?';
        }
    }

    private function className(Node $class): ?string
    {
        if (!$class instanceof Node\Name) {
            return $class instanceof Stmt\Class_ ? 'class@anonymous' : null;
        }
        $name = $class->toString();
        $lower = strtolower($name);
        if (in_array($lower, ['self', 'static'], true) && $this->classStack) {
            return $this->currentClass()['fqn'];
        }
        if ($lower === 'parent' && $this->classStack) {
            return $this->currentClass()['extends'][0] ?? 'parent';
        }
        return $name;
    }

    private function identifier(Node $name): ?string
    {
        return $name instanceof Node\Identifier || $name instanceof Node\Name ? $name->toString() : null;
    }
}
