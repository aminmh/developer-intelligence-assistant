<?php

declare(strict_types=1);

namespace App\CodeParser;

use PhpParser\Error;
use PhpParser\Node;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitor\NameResolver;
use PhpParser\Parser;
use PhpParser\ParserFactory;
use Throwable;

/**
 * The result of parsing one PHP source document.
 *
 * Parsing is deliberately represented as a value instead of throwing for
 * syntax errors. Callers that process a workspace can therefore skip one
 * invalid file without making the whole scan fail, while still retaining a
 * useful diagnostic for reporting.
 */
final readonly class PhpAstParseResult
{
    /**
     * @param Node\Stmt[] $nodes
     */
    private function __construct(
        public array $nodes,
        public ?string $errorMessage = null,
        public ?int $errorLine = null,
    ) {}

    /**
     * @param Node\Stmt[] $nodes
     */
    public static function success(array $nodes): self
    {
        return new self($nodes);
    }

    public static function failure(Throwable $error): self
    {
        return new self(
            nodes: [],
            errorMessage: $error instanceof Error ? $error->getRawMessage() : $error->getMessage(),
            errorLine: $error instanceof Error ? $error->getStartLine() : null,
        );
    }

    public function isSuccessful(): bool
    {
        return $this->errorMessage === null;
    }
}

/**
 * Parses PHP source and resolves names before handing the AST to consumers.
 *
 * Keeping parser construction, error handling, and name resolution here gives
 * the chunker and inspection tools one consistent parser lifecycle. The
 * parser still returns a safe result for malformed or otherwise unsupported
 * source instead of leaking parser exceptions into workspace scans.
 */
final readonly class PhpAstParser
{
    private Parser $parser;

    public function __construct(?Parser $parser = null)
    {
        $this->parser = $parser ?? (new ParserFactory())->createForNewestSupportedVersion();
    }

    /**
     * Parse and name-resolve a PHP source document.
     */
    public function parse(string $source): PhpAstParseResult
    {
        try {
            $ast = $this->parser->parse($source);
            if ($ast === null) {
                return PhpAstParseResult::failure(new \RuntimeException('PHP parser returned no AST'));
            }

            $traverser = new NodeTraverser();
            $traverser->addVisitor(new NameResolver());

            /** @var Node\Stmt[] $resolvedAst */
            $resolvedAst = $traverser->traverse($ast);

            return PhpAstParseResult::success($resolvedAst);
        } catch (Throwable $error) {
            return PhpAstParseResult::failure($error);
        }
    }
}
