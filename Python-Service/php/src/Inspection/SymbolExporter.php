<?php

declare(strict_types=1);

namespace App\Inspection;

use App\CodeParser\PhpAstParser;
use PhpParser\Node;
use PhpParser\Node\Expr;
use PhpParser\Node\Name;
use PhpParser\Node\Stmt;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitorAbstract;

/**
 * Tier-4 project-wide symbol exporter.
 *
 * Every file is traversed TWICE: once with NameResolver (replacing Names with
 * fully-qualified ones — so extends/implements/new/static-call targets are
 * resolved before collection) and once with the collecting visitor.
 *
 *  - `symbols`   → definitions: class/interface/trait/enum, method, property,
 *                  class const, free function — FQN, file, line range, plus
 *                  structural relation edges (extends/implements/uses_trait).
 *  - `references`→ static use-sites: new-instance, static calls, class-const
 *                  reads, instanceof, catch types, plain function calls, and
 *                  method calls with a best-effort receiver type.
 *
 * Receiver inference (honest, file-local only): inside each method/closure we
 * track the type of ``$this``, typed parameters (incl. promoted constructor
 * properties), typed properties, and variables assigned from ``new X`` /
 * ``$this->prop`` / another typed variable. A ``method_call`` is only linked
 * when that receiver type is known — everything else stays ``unresolved`` with
 * ``to_fqn`` null rather than guessing.
 *
 * Optional extra excludes (e.g. ``tests``) narrow the scanned tree.
 */
final class SymbolExporter
{
    private PhpAstParser $astParser;

    private const SKIP_DIRS = [
        'vendor', 'node_modules', '.git', '__MACOSX', '.idea', '.vscode',
        'dist', 'build', 'coverage', '.venv', 'var/cache', 'bootstrap/cache',
    ];

    public function __construct(?PhpAstParser $astParser = null)
    {
        $this->astParser = $astParser ?? new PhpAstParser();
    }

    /** @param list<string> $exclude extra relative dir segments to skip */
    public function phpFiles(string $rootPath, array $exclude = []): array
    {
        $skip = array_merge(self::SKIP_DIRS, $exclude);
        $out = [];
        $iterator = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($rootPath, \FilesystemIterator::SKIP_DOTS)
        );
        foreach ($iterator as $file) {
            if ($file->isFile() && $file->getExtension() === 'php') {
                $path = $file->getPathname();
                $skipped = false;
                foreach ($skip as $dir) {
                    if ($dir !== ''
                        && str_contains($path, DIRECTORY_SEPARATOR . $dir . DIRECTORY_SEPARATOR)
                    ) {
                        $skipped = true;
                        break;
                    }
                }
                if (!$skipped) {
                    $out[] = $path;
                }
            }
        }
        sort($out);
        return $out;
    }

    /** @return array<int,array<string,mixed>> definition records */
    public function exportDefinitions(string $rootPath, array $exclude = []): array
    {
        $defs = [];
        foreach ($this->phpFiles($rootPath, $exclude) as $file) {
            $ast = $this->parseResolved($file);
            if ($ast === null) {
                continue;
            }
            $collector = new class($file) extends NodeVisitorAbstract {
                public array $defs = [];
                private ?string $classFqn = null;

                public function __construct(private readonly string $file)
                {
                }

                public function enterNode(Node $node): mixed
                {
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
                        foreach ($node->props as $p) {
                            $this->defs[] = [
                                'kind' => 'property',
                                'name' => '$' . $p->name->toString(),
                                'fqn' => ($owner ?? '') . '::$' . $p->name->toString(),
                                'file' => $this->file,
                                'line' => $p->getStartLine(),
                                'end_line' => $p->getEndLine(),
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
                        foreach ($node->consts as $c) {
                            $this->defs[] = [
                                'kind' => 'class_const',
                                'name' => $c->name->toString(),
                                'fqn' => ($owner ?? '') . '::' . $c->name->toString(),
                                'file' => $this->file,
                                'line' => $c->getStartLine(),
                                'end_line' => $c->getEndLine(),
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
                        foreach ($node->consts as $c) {
                            $this->defs[] = [
                                'kind' => 'const',
                                'name' => $c->name->toString(),
                                'fqn' => $this->nsName($node) . '\\' . $c->name->toString(),
                                'file' => $this->file,
                                'line' => $c->getStartLine(),
                                'end_line' => $c->getEndLine(),
                                'visibility' => null,
                                'static' => null,
                                'abstract' => null,
                                'owner' => null,
                                'relations' => null,
                            ];
                        }
                        return null;
                    }
                    return null;
                }

                public function leaveNode(Node $node): mixed
                {
                    if ($node instanceof Stmt\ClassLike) {
                        $this->classFqn = null;
                    }
                    return null;
                }

                /** @return list<array{kind:string,target:string}> */
                private function relations(Stmt\ClassLike $node): array
                {
                    $rel = [];
                    if ($node instanceof Stmt\Class_) {
                        if ($node->extends !== null) {
                            $rel[] = ['kind' => 'extends', 'target' => $node->extends->toString()];
                        }
                        foreach ($node->implements as $i) {
                            $rel[] = ['kind' => 'implements', 'target' => $i->toString()];
                        }
                    } elseif ($node instanceof Stmt\Interface_) {
                        foreach ($node->extends as $i) {
                            $rel[] = ['kind' => 'extends', 'target' => $i->toString()];
                        }
                    } elseif ($node instanceof Stmt\Enum_) {
                        foreach ($node->implements as $i) {
                            $rel[] = ['kind' => 'implements', 'target' => $i->toString()];
                        }
                    }
                    foreach ($node->stmts as $member) {
                        if ($member instanceof Stmt\TraitUse) {
                            foreach ($member->traits as $t) {
                                $rel[] = ['kind' => 'uses_trait', 'target' => $t->toString()];
                            }
                        }
                    }
                    return $rel;
                }

                private function fqn(Stmt\ClassLike|Stmt\Function_ $node): string
                {
                    $ns = $node->namespacedName;
                    return $ns !== null ? $ns->toString() : '';
                }

                private function nsName(Node $node): string
                {
                    $ns = $node->namespacedName;
                    return $ns !== null ? $ns->toString() : '';
                }

                private function visibility(int $flags): string
                {
                    if ($flags & Stmt\Class_::MODIFIER_PUBLIC) return 'public';
                    if ($flags & Stmt\Class_::MODIFIER_PROTECTED) return 'protected';
                    if ($flags & Stmt\Class_::MODIFIER_PRIVATE) return 'private';
                    return 'public';
                }
            };

            $traverser = new NodeTraverser();
            $traverser->addVisitor($collector);
            $traverser->traverse($ast);
            $defs = array_merge($defs, $collector->defs);
        }
        return $defs;
    }

    /** @return array<int,array<string,mixed>> reference records */
    public function exportReferences(string $rootPath, array $exclude = []): array
    {
        $refs = [];
        foreach ($this->phpFiles($rootPath, $exclude) as $file) {
            $ast = $this->parseResolved($file);
            if ($ast === null) {
                continue;
            }
            $collector = new class($file) extends NodeVisitorAbstract {
                public array $refs = [];
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
                            $this->scope = $node->namespacedName?->toString()
                                ?: $node->name->toString();
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
                        foreach ($node->types as $t) {
                            $this->add('catch', $t->toString(), null, $line);
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
                        return null;
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

                // --- receiver inference ------------------------------------

                /** @param array<int,Node\Param> $params */
                private function pushScope(array $params): void
                {
                    $vars = [];
                    $origins = [];
                    foreach ($params as $p) {
                        if (!$p->var instanceof Expr\Variable
                            || !is_string($p->var->name)
                        ) {
                            continue;
                        }
                        $type = $this->typeToString($p->type);
                        if ($type === null) {
                            continue;
                        }
                        $vars['$' . $p->var->name] = $type;
                        $origins['$' . $p->var->name] = 'param';
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
                            foreach ($member->props as $p) {
                                $types['$' . $p->name->toString()] = $declared;
                            }
                        }
                        if ($member instanceof Stmt\ClassMethod
                            && $member->name->toString() === '__construct'
                        ) {
                            foreach ($member->params as $p) {
                                if (!($p->flags & (Stmt\Class_::MODIFIER_PUBLIC
                                        | Stmt\Class_::MODIFIER_PROTECTED
                                        | Stmt\Class_::MODIFIER_PRIVATE))
                                    || !$p->var instanceof Expr\Variable
                                    || !is_string($p->var->name)
                                ) {
                                    continue;
                                }
                                $declared = $this->typeToString($p->type);
                                if ($declared !== null) {
                                    $types['$' . $p->var->name] = $declared;
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
                    $frame = $this->varStack[count($this->varStack) - 1] ?? null;
                    if ($frame === null) {
                        return;
                    }
                    $frame[$key] = $type;
                    $this->varStack[count($this->varStack) - 1] = $frame;
                    $originFrame = $this->originStack[count($this->originStack) - 1];
                    $originFrame[$key] = $via ?? 'local';
                    $this->originStack[count($this->originStack) - 1] = $originFrame;
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
                        $type = $frame[$key] ?? null;
                        $origins = $this->originStack[count($this->originStack) - 1] ?? [];
                        return [$type, $origins[$key] ?? null];
                    }
                    if ($expr instanceof Expr\PropertyFetch) {
                        if ($expr->var instanceof Expr\Variable && $expr->var->name === 'this') {
                            $name = $expr->name instanceof Node\Identifier ? $expr->name->toString() : '';
                            $type = $this->propTypes['$' . $name] ?? null;
                            return [$type, $type !== null ? 'property' : null];
                        }
                        // $other->prop where $other is a typed local: needs the
                        // *other class's* property map — not file-local → skip.
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

                /** Resolve a type AST node to a single FQN (nullable/union unwrap). */
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
                        return null;
                    }
                    return null;
                }

                // --- records -------------------------------------------------

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
            };

            $traverser = new NodeTraverser();
            $traverser->addVisitor($collector);
            $traverser->traverse($ast);
            $refs = array_merge($refs, $collector->refs);
        }
        return $refs;
    }

    /** Parse a file and fully resolve names. Returns null when unparseable. */
    private function parseResolved(string $file): ?array
    {
        $source = file_get_contents($file);
        if ($source === false) {
            return null;
        }
        $result = $this->astParser->parse($source);
        return $result->isSuccessful() ? $result->nodes : null;
    }
}
