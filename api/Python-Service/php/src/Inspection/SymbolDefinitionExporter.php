<?php

declare(strict_types=1);

namespace App\Inspection;

use App\CodeParser\PhpAstParser;
use PhpParser\Node;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitorAbstract;
use PhpParser\Node\Stmt;

/** Exports declaration and structural relation records from resolved ASTs. */
final class SymbolDefinitionExporter
{
    public function __construct(private readonly PhpAstParser $astParser)
    {
    }

    /** @param list<string> $files @return array<int,array<string,mixed>> */
    public function export(array $files): array
    {
        $defs = [];
        foreach ($files as $file) {
            $source = file_get_contents($file);
            if ($source === false) {
                continue;
            }
            $result = $this->astParser->parse($source);
            if (!$result->isSuccessful()) {
                continue;
            }
            $collector = new SymbolDefinitionVisitor($file);
            $traverser = new NodeTraverser();
            $traverser->addVisitor($collector);
            $traverser->traverse($result->nodes);
            $defs = array_merge($defs, $collector->definitions());
        }
        return $defs;
    }
}

/** @internal Visitor implementation for SymbolDefinitionExporter. */
final class SymbolDefinitionVisitor extends NodeVisitorAbstract
{
    /** @var list<array<string,mixed>> */
    private array $defs = [];
    private ?string $classFqn = null;
    private ?string $namespace = null;

    public function __construct(private readonly string $file)
    {
    }

    /** @return list<array<string,mixed>> */
    public function definitions(): array
    {
        return $this->defs;
    }

    public function enterNode(Node $node): mixed
    {
        if ($node instanceof Stmt\Namespace_) {
            $this->namespace = $node->name?->toString();
            return null;
        }
        if ($node instanceof Stmt\ClassLike) {
            $fqn = $this->fqn($node);
            $this->defs[] = [
                'kind' => $node instanceof Stmt\Interface_ ? 'interface'
                    : ($node instanceof Stmt\Trait_ ? 'trait'
                        : ($node instanceof Stmt\Enum_ ? 'enum' : 'class')),
                'name' => $node->name?->toString() ?? '',
                'fqn' => $fqn,
                'file' => $this->file,
                'line' => $node->getStartLine(),
                'end_line' => $node->getEndLine(),
                'visibility' => null,
                'static' => null,
                'abstract' => null,
                'owner' => null,
                'relations' => $this->relations($node),
            ];
            $this->classFqn = $fqn;
            return null;
        }
        if ($node instanceof Stmt\ClassMethod) {
            $owner = $this->classFqn;
            $this->defs[] = [
                'kind' => 'method',
                'name' => $node->name->toString(),
                'fqn' => ($owner ?? '') . '::' . $node->name->toString(),
                'file' => $this->file,
                'line' => $node->getStartLine(),
                'end_line' => $node->getEndLine(),
                'visibility' => $node->isPublic() ? 'public'
                    : ($node->isProtected() ? 'protected' : 'private'),
                'static' => $node->isStatic(),
                'abstract' => $node->isAbstract(),
                'owner' => $owner,
                'relations' => null,
            ];
            return null;
        }
        if ($node instanceof Stmt\Property) {
            $owner = $this->classFqn;
            foreach ($node->props as $property) {
                $this->defs[] = [
                    'kind' => 'property',
                    'name' => '$' . $property->name->toString(),
                    'fqn' => ($owner ?? '') . '::$' . $property->name->toString(),
                    'file' => $this->file,
                    'line' => $property->getStartLine(),
                    'end_line' => $property->getEndLine(),
                    'visibility' => $this->visibility($node->flags),
                    'static' => (bool) ($node->flags & Stmt\Class_::MODIFIER_STATIC),
                    'abstract' => null,
                    'owner' => $owner,
                    'relations' => null,
                ];
            }
            return null;
        }
        if ($node instanceof Stmt\ClassConst) {
            $owner = $this->classFqn;
            foreach ($node->consts as $constant) {
                $this->defs[] = [
                    'kind' => 'class_const',
                    'name' => $constant->name->toString(),
                    'fqn' => ($owner ?? '') . '::' . $constant->name->toString(),
                    'file' => $this->file,
                    'line' => $constant->getStartLine(),
                    'end_line' => $constant->getEndLine(),
                    'visibility' => $this->visibility($node->flags),
                    'static' => null,
                    'abstract' => null,
                    'owner' => $owner,
                    'relations' => null,
                ];
            }
            return null;
        }
        if ($node instanceof Stmt\Function_) {
            $name = $node->name->toString();
            $this->defs[] = [
                'kind' => 'function',
                'name' => $name,
                'fqn' => $this->fqn($node) ?: $name,
                'file' => $this->file,
                'line' => $node->getStartLine(),
                'end_line' => $node->getEndLine(),
                'visibility' => null,
                'static' => null,
                'abstract' => null,
                'owner' => null,
                'relations' => null,
            ];
            return null;
        }
        if ($node instanceof Stmt\Const_) {
            foreach ($node->consts as $constant) {
                $this->defs[] = [
                    'kind' => 'const',
                    'name' => $constant->name->toString(),
                    'fqn' => $this->namespace
                        ? $this->namespace . '\\' . $constant->name->toString()
                        : $constant->name->toString(),
                    'file' => $this->file,
                    'line' => $constant->getStartLine(),
                    'end_line' => $constant->getEndLine(),
                    'visibility' => null,
                    'static' => null,
                    'abstract' => null,
                    'owner' => null,
                    'relations' => null,
                ];
            }
        }
        return null;
    }

    public function leaveNode(Node $node): mixed
    {
        if ($node instanceof Stmt\ClassLike) {
            $this->classFqn = null;
        }
        if ($node instanceof Stmt\Namespace_) {
            $this->namespace = null;
        }
        return null;
    }

    /** @return list<array{kind:string,target:string}> */
    private function relations(Stmt\ClassLike $node): array
    {
        $relations = [];
        if ($node instanceof Stmt\Class_) {
            if ($node->extends !== null) {
                $relations[] = ['kind' => 'extends', 'target' => $node->extends->toString()];
            }
            foreach ($node->implements as $interface) {
                $relations[] = ['kind' => 'implements', 'target' => $interface->toString()];
            }
        } elseif ($node instanceof Stmt\Interface_) {
            foreach ($node->extends as $interface) {
                $relations[] = ['kind' => 'extends', 'target' => $interface->toString()];
            }
        } elseif ($node instanceof Stmt\Enum_) {
            foreach ($node->implements as $interface) {
                $relations[] = ['kind' => 'implements', 'target' => $interface->toString()];
            }
        }
        foreach ($node->stmts as $member) {
            if ($member instanceof Stmt\TraitUse) {
                foreach ($member->traits as $trait) {
                    $relations[] = ['kind' => 'uses_trait', 'target' => $trait->toString()];
                }
            }
        }
        return $relations;
    }

    private function fqn(Stmt\ClassLike|Stmt\Function_ $node): string
    {
        return $node->namespacedName?->toString() ?? '';
    }

    private function visibility(int $flags): string
    {
        if ($flags & Stmt\Class_::MODIFIER_PUBLIC) return 'public';
        if ($flags & Stmt\Class_::MODIFIER_PROTECTED) return 'protected';
        if ($flags & Stmt\Class_::MODIFIER_PRIVATE) return 'private';
        return 'public';
    }
}
