<?php

declare(strict_types=1);

namespace App\Inspection;

use App\CodeParser\PhpAstParser;
use PhpParser\Node;
use PhpParser\Node\Stmt;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitorAbstract;
use PhpParser\PrettyPrinter\Standard as PrettyPrinter;

/**
 * Tier-2 structural skeleton exporter.
 *
 * Returns the API surface of a PHP file — class-like headers, properties,
 * constants and method signatures — with bodies stripped, so callers can
 * navigate a file without consuming implementation detail.
 *
 * Output shape (single JSON object):
 *   { "file": "...", "namespace": "App\\...|null",
 *     "declarations": [ { "kind": "class|interface|trait|enum",
 *       "name": "Foo", "fqn": "App\\Foo", "line": 7,
 *       "modifiers": ["final","abstract","readonly"],
 *       "extends": "App\\Bar|null", "implements": ["...","..."],
 *       "doc_summary": "first docblock line|null",
 *       "properties": [ {"name":"x","type":"?int","visibility":"private",
 *                        "static":false,"line":10,"doc_summary":null} ],
 *       "constants":  [ {"name":"X","value_summary":"'a'","visibility":"public"} ],
 *       "methods":    [ {"name":"run","visibility":"public","static":false,
 *                        "abstract":false,"final":false,
 *                        "params":[{"name":"$arg","type":"?string","default":null}],
 *                        "return_type":"int|null","line":14,
 *                        "doc_summary":"Do the thing"} ] } ] }
 */
final class SkeletonExporter
{
    private PhpAstParser $astParser;
    private PrettyPrinter $printer;

    public function __construct(?PhpAstParser $astParser = null)
    {
        $this->astParser = $astParser ?? new PhpAstParser();
        $this->printer = new PrettyPrinter();
    }

    /** @return array<string,mixed> */
    public function skeletonFile(string $absolutePath): array
    {
        $source = file_get_contents($absolutePath);
        if ($source === false) {
            throw new \RuntimeException("Cannot read file: $absolutePath");
        }
        return $this->skeletonSource($source, $absolutePath);
    }

    /** @return array<string,mixed> */
    public function skeletonSource(string $source, string $filePath = ''): array
    {
        $ast = $this->parse($source);
        $namespace = null;
        $declarations = [];

        $collector = new class($source, $this->printer) extends NodeVisitorAbstract {
            /** @var array<int,array<string,mixed>> */
            public array $declarations = [];
            public ?string $namespace = null;

            public function __construct(
                private readonly string $source,
                private readonly PrettyPrinter $printer,
            ) {
            }

            public function enterNode(Node $node): mixed
            {
                if ($node instanceof Stmt\Namespace_) {
                    $this->namespace = $node->name?->toString();
                    return null;
                }
                if ($node instanceof Stmt\ClassLike) {
                    $this->declarations[] = $this->classLike($node);
                }
                return null;
            }

            /** @return array<string,mixed> */
            private function classLike(Stmt\ClassLike $class): array
            {
                $ns = (string) ($class->namespacedName ?? $class->name ?? '');
                $summary = $this->docSummary($class);

                $extends = null;
                $implements = [];
                $kind = 'class';
                $modifiers = [];

                if ($class instanceof Stmt\Class_) {
                    $kind = 'class';
                    $modifiers = $this->flags($class->flags, ['final', 'abstract', 'readonly']);
                    $extends = $class->extends?->toString();
                    foreach ($class->implements as $i) {
                        $implements[] = $i->toString();
                    }
                } elseif ($class instanceof Stmt\Interface_) {
                    $kind = 'interface';
                    foreach ($class->extends as $i) {
                        $implements[] = $i->toString();
                    }
                } elseif ($class instanceof Stmt\Trait_) {
                    $kind = 'trait';
                } elseif ($class instanceof Stmt\Enum_) {
                    $kind = 'enum';
                    foreach ($class->implements as $i) {
                        $implements[] = $i->toString();
                    }
                }

                $properties = [];
                $constants = [];
                $methods = [];
                $usedTraits = [];

                foreach ($class->stmts as $member) {
                    if ($member instanceof Stmt\Property) {
                        foreach ($member->props as $prop) {
                            $properties[] = [
                                'name' => '$' . $prop->name->toString(),
                                'type' => $member->type !== null
                                    ? $this->printer->prettyPrint([$member->type])
                                    : null,
                                'visibility' => $this->visibility($member->flags),
                                'static' => (bool) ($member->flags & Stmt\Class_::MODIFIER_STATIC),
                                'readonly' => (bool) ($member->flags & Stmt\Class_::MODIFIER_READONLY),
                                'line' => $member->getStartLine(),
                                'doc_summary' => $this->docSummary($member),
                            ];
                        }
                    } elseif ($member instanceof Stmt\ClassConst) {
                        foreach ($member->consts as $c) {
                            $constants[] = [
                                'name' => $c->name->toString(),
                                'visibility' => $this->visibility($member->flags),
                                'final' => (bool) ($member->flags & Stmt\Class_::MODIFIER_FINAL),
                            ];
                        }
                    } elseif ($member instanceof Stmt\ClassMethod) {
                        $methods[] = [
                            'name' => $member->name->toString(),
                            'visibility' => $this->visibility($member->flags),
                            'static' => (bool) ($member->flags & Stmt\Class_::MODIFIER_STATIC),
                            'abstract' => (bool) ($member->flags & Stmt\Class_::MODIFIER_ABSTRACT),
                            'final' => (bool) ($member->flags & Stmt\Class_::MODIFIER_FINAL),
                            'params' => $this->params($member),
                            'return_type' => $member->returnType !== null
                                ? $this->printer->prettyPrint([$member->returnType])
                                : null,
                            'line' => $member->getStartLine(),
                            'doc_summary' => $this->docSummary($member),
                        ];
                    } elseif ($member instanceof Stmt\TraitUse) {
                        foreach ($member->traits as $t) {
                            $usedTraits[] = $t->toString();
                        }
                    }
                }

                return [
                    'kind' => $kind,
                    'name' => (string) ($class->name ?? ''),
                    'fqn' => $ns,
                    'line' => $class->getStartLine(),
                    'modifiers' => $modifiers,
                    'extends' => $extends,
                    'extends_list' => $class instanceof Stmt\Interface_ ? $implements : [],
                    'implements' => $class instanceof Stmt\Interface_ ? [] : $implements,
                    'uses_traits' => $usedTraits,
                    'doc_summary' => $summary,
                    'properties' => $properties,
                    'constants' => $constants,
                    'methods' => $methods,
                ];
            }

            /** @return array<int,array<string,mixed>> */
            private function params(Stmt\ClassMethod $method): array
            {
                $out = [];
                foreach ($method->params as $p) {
                    $default = null;
                    if ($p->default !== null) {
                        $default = $this->printer->prettyPrintExpr($p->default);
                    }
                    $out[] = [
                        'name' => '$' . $p->var->name,
                        'type' => $p->type !== null ? $this->printer->prettyPrint([$p->type]) : null,
                        'default' => $default,
                    ];
                }
                return $out;
            }

            private function visibility(int $flags): string
            {
                if ($flags & Stmt\Class_::MODIFIER_PUBLIC) return 'public';
                if ($flags & Stmt\Class_::MODIFIER_PROTECTED) return 'protected';
                if ($flags & Stmt\Class_::MODIFIER_PRIVATE) return 'private';
                return 'public';
            }

            /** @return list<string> */
            private function flags(int $flags, array $names): array
            {
                $out = [];
                $map = [
                    Stmt\Class_::MODIFIER_FINAL => 'final',
                    Stmt\Class_::MODIFIER_ABSTRACT => 'abstract',
                    Stmt\Class_::MODIFIER_READONLY => 'readonly',
                ];
                foreach ($names as $n) {
                    foreach ($map as $bit => $label) {
                        if ($label === $n && ($flags & $bit)) {
                            $out[] = $n;
                        }
                    }
                }
                return $out;
            }

            private function docSummary(Node $node): ?string
            {
                $doc = $node->getDocComment();
                if ($doc === null) {
                    return null;
                }
                $text = $doc->getText();
                $text = trim((string) preg_replace('/^\s*\/\*\*|\*\/$|\*\s?/m', '', $text));
                $first = trim((string) preg_split('/\r?\n/', $text, 2)[0]);
                return $first === '' ? null : substr($first, 0, 200);
            }
        };

        $traverser = new NodeTraverser();
        $traverser->addVisitor($collector);
        $traverser->traverse($ast);

        return [
            'file' => $filePath,
            'namespace' => $collector->namespace,
            'declarations' => $collector->declarations,
        ];
    }

    private function parse(string $source): array
    {
        return $this->astParser->parse($source)->nodes;
    }
}
