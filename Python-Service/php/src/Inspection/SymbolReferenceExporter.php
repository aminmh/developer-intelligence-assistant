<?php

declare(strict_types=1);

namespace App\Inspection;

use App\CodeParser\PhpAstParser;
use PhpParser\Node;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitorAbstract;
use PhpParser\Node\Expr;
use PhpParser\Node\Name;
use PhpParser\Node\Stmt;

/** Exports static use-site records from resolved ASTs. */
final class SymbolReferenceExporter
{
    public function __construct(private readonly PhpAstParser $astParser)
    {
    }

    /** @param list<string> $files @return array<int,array<string,mixed>> */
    public function export(array $files): array
    {
        $refs = [];
        foreach ($files as $file) {
            $source = file_get_contents($file);
            if ($source === false) {
                continue;
            }
            $result = $this->astParser->parse($source);
            if (!$result->isSuccessful()) {
                continue;
            }
            $collector = new SymbolReferenceVisitor($file);
            $traverser = new NodeTraverser();
            $traverser->addVisitor($collector);
            $traverser->traverse($result->nodes);
            $refs = array_merge($refs, $collector->references());
        }
        return $refs;
    }
}

/** @internal Visitor implementation for SymbolReferenceExporter. */
final class SymbolReferenceVisitor extends NodeVisitorAbstract
{
    /** @var list<array<string,mixed>> */
    private array $refs = [];
    private ?string $scope = null;
    private ?string $classFqn = null;
    private ?string $extendsTarget = null;

    /** @var array<string,string> declared property types ($x → FQN) */
    private array $propTypes = [];

    /** @var array<int,array<string,string>> scoped variable types */
    private array $varStack = [];

    /** @var array<int,array<string,string>> scoped variable origins */
    private array $originStack = [];

    public function __construct(private readonly string $file)
    {
    }

    /** @return list<array<string,mixed>> */
    public function references(): array
    {
        return $this->refs;
    }

    public function enterNode(Node $node): mixed
    {
        if ($node instanceof Stmt\ClassLike) {
            $this->classFqn = $node->namespacedName?->toString()
                ?? ($node->name?->toString() ?? '');
            if ($node instanceof Stmt\Class_ && $node->extends !== null) {
                $this->extendsTarget = $node->extends->toString();
            }
            $this->propTypes = $this->propertyTypes($node);
            return null;
        }
        if ($node instanceof Stmt\ClassMethod) {
            $this->scope = ($this->classFqn ?? '') . '::' . $node->name->toString();
            $this->pushScope($node->params);
            return null;
        }
        if ($node instanceof Stmt\Function_ || $node instanceof Expr\Closure
            || $node instanceof Expr\ArrowFunction
        ) {
            if ($node instanceof Stmt\Function_) {
                $this->scope = $node->namespacedName?->toString() ?: $node->name->toString();
            }
            $this->pushScope($node->params);
            return null;
        }

        $line = $node->getStartLine();
        if ($node instanceof Expr\Assign || $node instanceof Expr\AssignRef) {
            $this->trackAssignment($node->var, $node->expr);
            return null;
        }
        if ($node instanceof Expr\New_) {
            if ($node->class instanceof Name) {
                $this->add('new', $node->class->toString(), null, $line);
            }
            return null;
        }
        if ($node instanceof Expr\StaticCall) {
            if ($node->class instanceof Name) {
                $this->add(
                    'static_call',
                    $this->resolveSelfish($node->class),
                    $node->name instanceof Node\Identifier ? $node->name->toString() : null,
                    $line,
                );
            }
            return null;
        }
        if ($node instanceof Expr\ClassConstFetch) {
            if ($node->class instanceof Name) {
                $this->add(
                    'class_const',
                    $this->resolveSelfish($node->class),
                    $node->name instanceof Node\Identifier ? $node->name->toString() : null,
                    $line,
                );
            }
            return null;
        }
        if ($node instanceof Expr\FuncCall) {
            if ($node->name instanceof Name) {
                $this->add('function_call', $node->name->toString(), null, $line);
            }
            return null;
        }
        if ($node instanceof Expr\Instanceof_) {
            if ($node->class instanceof Name) {
                $this->add('instanceof', $node->class->toString(), null, $line);
            }
            return null;
        }
        if ($node instanceof Stmt\Catch_) {
            foreach ($node->types as $type) {
                $this->add('catch', $type->toString(), null, $line);
            }
            return null;
        }
        if ($node instanceof Expr\MethodCall || $node instanceof Expr\NullsafeMethodCall) {
            if ($node->name instanceof Node\Identifier) {
                [$type, $via] = $this->receiverType($node->var);
                if ($type !== null && $type !== '') {
                    $this->add(
                        'method_call',
                        $type,
                        $node->name->toString(),
                        $line,
                        false,
                        $via,
                    );
                } else {
                    $this->add('method_call', null, $node->name->toString(), $line, true);
                }
            }
        }
        return null;
    }

    public function leaveNode(Node $node): mixed
    {
        if ($node instanceof Stmt\ClassMethod || $node instanceof Stmt\Function_
            || $node instanceof Expr\Closure || $node instanceof Expr\ArrowFunction
        ) {
            $this->scope = null;
            $this->popScope();
        }
        if ($node instanceof Stmt\ClassLike) {
            $this->classFqn = null;
            $this->extendsTarget = null;
            $this->propTypes = [];
        }
        return null;
    }

    /** @param array<int,Node\Param> $params */
    private function pushScope(array $params): void
    {
        $vars = [];
        $origins = [];
        foreach ($params as $param) {
            if (!$param->var instanceof Expr\Variable || !is_string($param->var->name)) {
                continue;
            }
            $type = $this->typeToString($param->type);
            if ($type === null) {
                continue;
            }
            $vars['$' . $param->var->name] = $type;
            $origins['$' . $param->var->name] = 'param';
        }
        $this->varStack[] = $vars;
        $this->originStack[] = $origins;
    }

    private function popScope(): void
    {
        array_pop($this->varStack);
        array_pop($this->originStack);
    }

    /** @return array<string,string> declared+promoted property types */
    private function propertyTypes(Stmt\ClassLike $node): array
    {
        $types = [];
        foreach ($node->stmts as $member) {
            if ($member instanceof Stmt\Property) {
                $declared = $this->typeToString($member->type);
                if ($declared === null) {
                    continue;
                }
                foreach ($member->props as $property) {
                    $types['$' . $property->name->toString()] = $declared;
                }
            }
            if ($member instanceof Stmt\ClassMethod && $member->name->toString() === '__construct') {
                foreach ($member->params as $param) {
                    if (!($param->flags & (Stmt\Class_::MODIFIER_PUBLIC
                            | Stmt\Class_::MODIFIER_PROTECTED
                            | Stmt\Class_::MODIFIER_PRIVATE))
                        || !$param->var instanceof Expr\Variable
                        || !is_string($param->var->name)
                    ) {
                        continue;
                    }
                    $declared = $this->typeToString($param->type);
                    if ($declared !== null) {
                        $types['$' . $param->var->name] = $declared;
                    }
                }
            }
        }
        return $types;
    }

    private function trackAssignment(Expr $target, Expr $value): void
    {
        if (!$target instanceof Expr\Variable || !is_string($target->name)) {
            return;
        }
        $key = '$' . $target->name;
        [$type, $via] = $this->receiverType($value);
        if ($type === null || $type === '') {
            return;
        }
        $index = count($this->varStack) - 1;
        if ($index < 0) {
            return;
        }
        $frame = $this->varStack[$index];
        $frame[$key] = $type;
        $this->varStack[$index] = $frame;
        $originFrame = $this->originStack[$index];
        $originFrame[$key] = $via ?? 'local';
        $this->originStack[$index] = $originFrame;
    }

    /** @return array{0:string|null,1:string|null} [type, via] */
    private function receiverType(Expr $expr): array
    {
        if ($expr instanceof Expr\Variable) {
            if ($expr->name === 'this') {
                return [$this->classFqn, 'self'];
            }
            if (!is_string($expr->name)) {
                return [null, null];
            }
            $key = '$' . $expr->name;
            $frame = $this->varStack[count($this->varStack) - 1] ?? [];
            $origins = $this->originStack[count($this->originStack) - 1] ?? [];
            return [$frame[$key] ?? null, $origins[$key] ?? null];
        }
        if ($expr instanceof Expr\PropertyFetch) {
            if ($expr->var instanceof Expr\Variable && $expr->var->name === 'this') {
                $name = $expr->name instanceof Node\Identifier ? $expr->name->toString() : '';
                $type = $this->propTypes['$' . $name] ?? null;
                return [$type, $type !== null ? 'property' : null];
            }
            return [null, null];
        }
        if ($expr instanceof Expr\New_) {
            if ($expr->class instanceof Name) {
                return [$expr->class->toString(), 'new'];
            }
            return [null, null];
        }
        if ($expr instanceof Expr\Clone_) {
            return $this->receiverType($expr->expr);
        }
        return [null, null];
    }

    private function typeToString(?Node $type): ?string
    {
        while ($type instanceof Node\NullableType) {
            $type = $type->type;
        }
        if ($type instanceof Name) {
            return $type->toString();
        }
        if ($type instanceof Node\UnionType) {
            foreach ($type->types as $part) {
                if ($part instanceof Name) {
                    return $part->toString();
                }
            }
        }
        return null;
    }

    private function resolveSelfish(Name $name): string
    {
        $leaf = $name->toString();
        if ($leaf === 'self' || $leaf === 'static') {
            return $this->classFqn ?? $leaf;
        }
        if ($leaf === 'parent') {
            return $this->extendsTarget ?? 'parent';
        }
        return $leaf;
    }

    private function add(
        string $kind,
        ?string $toFqn,
        ?string $toMember,
        int $line,
        bool $unresolved = false,
        ?string $via = null,
    ): void {
        $row = [
            'kind' => $kind,
            'file' => $this->file,
            'line' => $line,
            'scope' => $this->scope,
            'to_fqn' => $toFqn,
            'to_member' => $toMember,
            'unresolved' => $unresolved,
        ];
        if ($kind === 'method_call') {
            $row['via'] = $via;
        }
        $this->refs[] = $row;
    }
}
