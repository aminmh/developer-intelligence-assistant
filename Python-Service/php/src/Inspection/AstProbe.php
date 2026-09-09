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
 * Tier-3 deep AST probe (nikic/php-parser).
 *
 * Two precise operations:
 *  - `attributes(file, className?)` → PHP 8 attributes (class + method level,
 *    e.g. #[Route(...)], #[AsEventListener]) resolved to FQNs with pretty-printed
 *    argument lists, plus docblock tag inventory (@template/@param/@return/…).
 *  - `method(file, methodName)` → the exact source text (signature + body) of
 *    one method, its structural signature, docblock, and byte location. Only the
 *    targeted method — never the whole file.
 */
final class AstProbe
{
    private PhpAstParser $astParser;
    private PrettyPrinter $printer;

    public function __construct(?PhpAstParser $astParser = null)
    {
        $this->astParser = $astParser ?? new PhpAstParser();
        $this->printer = new PrettyPrinter();
    }

    /**
     * @return array<string,mixed> { file, class_name|null, attributes:[...],
     *                               doc_tags:{...} }
     */
    public function attributes(string $absolutePath, ?string $className = null): array
    {
        $source = file_get_contents($absolutePath);
        if ($source === false) {
            throw new \RuntimeException("Cannot read file: $absolutePath");
        }
        $ast = $this->parse($source);

        $out = ['file' => $absolutePath, 'class_name' => $className, 'attributes' => []];
        $probe = new class($className, $this->printer) extends NodeVisitorAbstract {
            /** @var array<int,array<string,mixed>> */
            public array $attributes = [];

            public function __construct(
                private readonly ?string $className,
                private readonly PrettyPrinter $printer,
            ) {
            }

            public function enterNode(Node $node): mixed
            {
                if (!$node instanceof Stmt\ClassLike) {
                    return null;
                }
                if ($this->className !== null
                    && $node->name !== null
                    && $node->name->toString() !== $this->className
                ) {
                    return NodeTraverser::DONT_TRAVERSE_CHILDREN;
                }
                $this->collect($node, 'class', $node->name?->toString() ?? '');
                foreach ($node->stmts as $member) {
                    if ($member instanceof Stmt\ClassMethod) {
                        $this->collect($member, 'method', $member->name->toString());
                    }
                }
                return NodeTraverser::DONT_TRAVERSE_CHILDREN;
            }

            /** @return string */
            private function prettyArgs(array $args): string
            {
                $bits = [];
                foreach ($args as $arg) {
                    $value = $this->printer->prettyPrintExpr($arg->value);
                    $bits[] = $arg->name !== null ? $arg->name->toString() . ': ' . $value : $value;
                }
                return implode(', ', $bits);
            }

            private function collect(Stmt\ClassLike|Stmt\ClassMethod $node, string $kind, string $name): void
            {
                $groups = [];
                foreach ($node->attrGroups as $group) {
                    foreach ($group->attrs as $attr) {
                        $groups[] = [
                            'name' => $attr->name->toString(),
                            'args' => $this->prettyArgs($attr->args),
                            'line' => $attr->getStartLine(),
                        ];
                    }
                }
                $doc = $node->getDocComment();
                $this->attributes[] = [
                    'kind' => $kind,
                    'name' => $name,
                    'fqn' => ($node instanceof Stmt\ClassLike && $node->namespacedName !== null)
                        ? $node->namespacedName->toString()
                        : null,
                    'attributes' => $groups,
                    'doc_tags' => $doc === null ? [] : $this->docTags($doc->getText()),
                ];
            }

            /** @return array<string,list<string>> */
            private function docTags(string $doc): array
            {
                $tags = [];
                if (preg_match_all('/@([a-zA-Z_][a-zA-Z0-9_-]*)(?:\s+([^\s*]+))?/', $doc, $m, PREG_SET_ORDER)) {
                    foreach ($m as $hit) {
                        $name = strtolower($hit[1]);
                        $tags[$name][] = isset($hit[2]) && $hit[2] !== '' ? $hit[2] : '';
                    }
                }
                return $tags;
            }
        };

        $traverser = new NodeTraverser();
        $traverser->addVisitor($probe);
        $traverser->traverse($ast);

        $out['attributes'] = $probe->attributes;
        return $out;
    }

    /**
     * @return array<string,mixed>|null null when the method does not exist
     */
    public function method(string $absolutePath, string $methodName): ?array
    {
        $source = file_get_contents($absolutePath);
        if ($source === false) {
            throw new \RuntimeException("Cannot read file: $absolutePath");
        }
        $ast = $this->parse($source);

        $found = null;
        $probe = new class($methodName, $source, $this->printer) extends NodeVisitorAbstract {
            public mixed $found = null;

            public function __construct(
                private readonly string $methodName,
                private readonly string $source,
                private readonly PrettyPrinter $printer,
            ) {
            }

            public function enterNode(Node $node): mixed
            {
                if (!$node instanceof Stmt\ClassMethod
                    || $node->name->toString() !== $this->methodName
                ) {
                    return null;
                }
                $start = $node->getStartFilePos();
                $end = $node->getEndFilePos();
                $raw = substr($this->source, $start, $end - $start + 1);

                $params = [];
                foreach ($node->params as $p) {
                    $params[] = [
                        'name' => '$' . $p->var->name,
                        'type' => $p->type !== null
                            ? $this->printer->prettyPrint([$p->type])
                            : null,
                        'default' => $p->default !== null
                            ? $this->printer->prettyPrintExpr($p->default)
                            : null,
                    ];
                }

                $attrs = [];
                foreach ($node->attrGroups as $group) {
                    foreach ($group->attrs as $attr) {
                        $argBits = [];
                        foreach ($attr->args as $arg) {
                            $value = $this->printer->prettyPrintExpr($arg->value);
                            $argBits[] = $arg->name !== null
                                ? $arg->name->toString() . ': ' . $value
                                : $value;
                        }
                        $attrs[] = [
                            'name' => $attr->name->toString(),
                            'args' => implode(', ', $argBits),
                        ];
                    }
                }

                $this->found = [
                    'name' => $this->methodName,
                    'file' => null, // filled by caller
                    'start_line' => $node->getStartLine(),
                    'end_line' => $node->getEndLine(),
                    'visibility' => $node->isPublic() ? 'public'
                        : ($node->isProtected() ? 'protected' : 'private'),
                    'static' => $node->isStatic(),
                    'abstract' => $node->isAbstract(),
                    'final' => $node->isFinal(),
                    'params' => $params,
                    'return_type' => $node->returnType !== null
                        ? $this->printer->prettyPrint([$node->returnType])
                        : null,
                    'attributes' => $attrs,
                    'docblock' => $node->getDocComment()?->getText(),
                    'source' => $raw,
                ];
                return NodeTraverser::DONT_TRAVERSE_CHILDREN;
            }
        };

        $traverser = new NodeTraverser();
        $traverser->addVisitor($probe);
        $traverser->traverse($ast);

        if ($probe->found !== null) {
            $probe->found['file'] = $absolutePath;
        }
        return $probe->found;
    }

    private function parse(string $source): array
    {
        return $this->astParser->parse($source)->nodes;
    }
}
