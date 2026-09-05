<?php

declare(strict_types=1);

namespace App\Inspection;

use PhpParser\Error;
use PhpParser\Node;
use PhpParser\Node\Expr;
use PhpParser\Node\Name;
use PhpParser\Node\Stmt;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitorAbstract;
use PhpParser\ParserFactory;

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
 *                  reads, instanceof, catch types, plain function calls and
 *                  low-confidence bare method-call names (owner unresolvable).
 *
 * Vendorless by design: resolution is namespace + file scan based.
 */
final class SymbolExporter
{
    private \PhpParser\Parser $parser;

    private const SKIP_DIRS = [
        'vendor', 'node_modules', '.git', '__MACOSX', '.idea', '.vscode',
        'dist', 'build', 'coverage', '.venv', 'var/cache', 'bootstrap/cache',
    ];

    public function __construct()
    {
        $this->parser = (new ParserFactory())->createForNewestSupportedVersion();
    }

    /** @return list<string> */
    public function phpFiles(string $rootPath): array
    {
        $out = [];
        $iterator = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($rootPath, \FilesystemIterator::SKIP_DOTS)
        );
        foreach ($iterator as $file) {
            if ($file->isFile() && $file->getExtension() === 'php') {
                $path = $file->getPathname();
                $skip = false;
                foreach (self::SKIP_DIRS as $dir) {
                    if (str_contains($path, DIRECTORY_SEPARATOR . $dir . DIRECTORY_SEPARATOR)) {
                        $skip = true;
                        break;
                    }
                }
                if (!$skip) {
                    $out[] = $path;
                }
            }
        }
        sort($out);
        return $out;
    }

    /** @return array<int,array<string,mixed>> definition records */
    public function exportDefinitions(string $rootPath): array
    {
        $defs = [];
        foreach ($this->phpFiles($rootPath) as $file) {
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
                        $def = [
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
                        $this->defs[] = $def;
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
    public function exportReferences(string $rootPath): array
    {
        $refs = [];
        foreach ($this->phpFiles($rootPath) as $file) {
            $ast = $this->parseResolved($file);
            if ($ast === null) {
                continue;
            }
            $collector = new class($file) extends NodeVisitorAbstract {
                public array $refs = [];
                private ?string $scope = null;
                private ?string $classFqn = null;
                private ?string $extendsTarget = null;

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
                        return null;
                    }
                    if ($node instanceof Stmt\ClassMethod) {
                        $this->scope = ($this->classFqn ?? '') . '::' . $node->name->toString();
                        return null;
                    }
                    if ($node instanceof Stmt\Function_) {
                        $this->scope = $node->namespacedName?->toString() ?: $node->name->toString();
                        return null;
                    }

                    $line = $node->getStartLine();
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
                    if ($node instanceof Expr\MethodCall) {
                        if ($node->name instanceof Node\Identifier) {
                            $this->add('method_call', null, $node->name->toString(), $line, true);
                        }
                        return null;
                    }
                    return null;
                }

                public function leaveNode(Node $node): mixed
                {
                    if ($node instanceof Stmt\ClassMethod || $node instanceof Stmt\Function_) {
                        $this->scope = null;
                    }
                    if ($node instanceof Stmt\ClassLike) {
                        $this->classFqn = null;
                        $this->extendsTarget = null;
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
                ): void {
                    $this->refs[] = [
                        'kind' => $kind,
                        'file' => $this->file,
                        'line' => $line,
                        'scope' => $this->scope,
                        'to_fqn' => $toFqn,
                        'to_member' => $toMember,
                        'unresolved' => $unresolved,
                    ];
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
        try {
            $ast = $this->parser->parse($source) ?? [];
        } catch (Error $e) {
            return null;
        }
        if ($ast === []) {
            return $ast;
        }
        $resolver = new NodeTraverser();
        $resolver->addVisitor(new \PhpParser\NodeVisitor\NameResolver());
        $resolver->traverse($ast);
        return $ast;
    }
}
