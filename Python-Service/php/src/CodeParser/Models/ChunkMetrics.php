<?php

namespace App\CodeParser\Models;

use PhpParser\Node;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitorAbstract;

/** Deterministic `code-metrics-1` metrics computed from one AST node. */
final class ChunkMetrics
{
    public const SCHEMA = 'code-metrics-1';

    public static function contentHash(string $code): string
    {
        return hash('sha256', str_replace(["\r\n", "\r"], "\n", $code));
    }

    /**
     * @param callable(Node\Name): ?string $resolveName
     * @param Node[] $statements
     * @param Node\Param[] $params
     */
    public static function forCallable(string $code, array $statements, array $params, callable $resolveName): array
    {
        $visitor = new class($resolveName) extends NodeVisitorAbstract {
            public int $cyclomatic = 1;
            public int $depth = 0;
            public int $maxNesting = 0;
            public int $typeChecks = 0;
            public array $reads = [];
            public array $writes = [];
            public array $instantiates = [];
            public array $throws = [];
            private array $skipRead = [];
            private array $nestingIds = [];

            public function __construct(private $resolveName) {}

            public function enterNode(Node $node)
            {
                if ($node instanceof Node\Expr\Closure || $node instanceof Node\Expr\ArrowFunction
                    || $node instanceof Node\Stmt\Function_ || $node instanceof Node\Stmt\Class_) {
                    return NodeTraverser::DONT_TRAVERSE_CHILDREN;
                }
                if ($node instanceof Node\Stmt\If_ || $node instanceof Node\Stmt\ElseIf_
                    || $node instanceof Node\Stmt\For_ || $node instanceof Node\Stmt\Foreach_
                    || $node instanceof Node\Stmt\While_ || $node instanceof Node\Stmt\Do_
                    || $node instanceof Node\Stmt\Catch_
                    || $node instanceof Node\Expr\Ternary
                    || $node instanceof Node\Expr\BinaryOp\Coalesce
                    || $node instanceof Node\Expr\BinaryOp\BooleanAnd || $node instanceof Node\Expr\BinaryOp\BooleanOr
                    || $node instanceof Node\Expr\BinaryOp\LogicalAnd || $node instanceof Node\Expr\BinaryOp\LogicalOr) {
                    $this->cyclomatic++;
                }
                if ($node instanceof Node\Stmt\Case_ && $node->cond !== null) $this->cyclomatic++;
                if ($node instanceof Node\MatchArm && $node->conds !== null) $this->cyclomatic++;
                if ($node instanceof Node\Stmt\If_ || $node instanceof Node\Stmt\For_ || $node instanceof Node\Stmt\Foreach_
                    || $node instanceof Node\Stmt\While_ || $node instanceof Node\Stmt\Do_
                    || $node instanceof Node\Stmt\Switch_ || $node instanceof Node\Stmt\TryCatch) {
                    $this->nestingIds[spl_object_id($node)] = true;
                    $this->depth++;
                    $this->maxNesting = max($this->maxNesting, $this->depth);
                }
                if ($node instanceof Node\Expr\Instanceof_) $this->typeChecks++;
                if ($node instanceof Node\Expr\Assign || $node instanceof Node\Expr\AssignRef) {
                    $this->markWrite($node->var, false);
                } elseif ($node instanceof Node\Expr\AssignOp) {
                    $this->markWrite($node->var, true);
                } elseif ($node instanceof Node\Expr\PreInc || $node instanceof Node\Expr\PostInc
                    || $node instanceof Node\Expr\PreDec || $node instanceof Node\Expr\PostDec) {
                    $this->markWrite($node->var, true);
                }
                if ($node instanceof Node\Expr\PropertyFetch) {
                    $name = $this->thisProperty($node);
                    if ($name !== null && !isset($this->skipRead[spl_object_id($node)])) $this->reads[$name] = true;
                } elseif ($node instanceof Node\Expr\StaticPropertyFetch) {
                    $name = $this->staticProperty($node);
                    if ($name !== null && !isset($this->skipRead[spl_object_id($node)])) $this->reads[$name] = true;
                }
                if ($node instanceof Node\Expr\New_ && $node->class instanceof Node\Name) {
                    $resolved = $this->resolve($node->class);
                    if ($resolved !== null) $this->instantiates[$resolved] = true;
                }
                if ($node instanceof Node\Expr\Throw_ && $node->expr instanceof Node\Expr\New_
                    && $node->expr->class instanceof Node\Name) {
                    $resolved = $this->resolve($node->expr->class);
                    if ($resolved !== null) $this->throws[$resolved] = true;
                }
                return null;
            }

            public function leaveNode(Node $node)
            {
                if (isset($this->nestingIds[spl_object_id($node)])) {
                    unset($this->nestingIds[spl_object_id($node)]);
                    $this->depth--;
                }
                return null;
            }

            private function resolve(Node\Name $name): ?string
            {
                $raw = $name->toString();
                if (in_array(strtolower($raw), ['self', 'static', 'parent'], true)) return null;
                return ($this->resolveName)($name) ?? $raw;
            }

            private function markWrite(Node $target, bool $alsoRead): void
            {
                while ($target instanceof Node\Expr\ArrayDimFetch) $target = $target->var;
                $name = null;
                if ($target instanceof Node\Expr\PropertyFetch) $name = $this->thisProperty($target);
                elseif ($target instanceof Node\Expr\StaticPropertyFetch) $name = $this->staticProperty($target);
                if ($name === null) return;
                $this->writes[$name] = true;
                if (!$alsoRead) $this->skipRead[spl_object_id($target)] = true;
            }

            private function thisProperty(Node\Expr\PropertyFetch $node): ?string
            {
                if ($node->var instanceof Node\Expr\Variable && $node->var->name === 'this'
                    && $node->name instanceof Node\Identifier) return $node->name->toString();
                return null;
            }

            private function staticProperty(Node\Expr\StaticPropertyFetch $node): ?string
            {
                if ($node->class instanceof Node\Name
                    && in_array(strtolower($node->class->toString()), ['self', 'static'], true)
                    && $node->name instanceof Node\VarLikeIdentifier) return $node->name->toString();
                return null;
            }
        };
        $traverser = new NodeTraverser();
        $traverser->addVisitor($visitor);
        $traverser->traverse($statements);
        $sorted = static function (array $set): array {
            $keys = array_map('strval', array_keys($set));
            sort($keys, SORT_STRING);
            return $keys;
        };
        return [
            'metric_schema' => self::SCHEMA,
            'content_hash' => self::contentHash($code),
            'cyclomatic' => $visitor->cyclomatic,
            'max_nesting' => $visitor->maxNesting,
            'param_count' => count($params),
            'reads_properties' => $sorted($visitor->reads),
            'writes_properties' => $sorted($visitor->writes),
            'type_checks' => $visitor->typeChecks,
            'instantiates' => $sorted($visitor->instantiates),
            'throws' => $sorted($visitor->throws),
        ];
    }
}
