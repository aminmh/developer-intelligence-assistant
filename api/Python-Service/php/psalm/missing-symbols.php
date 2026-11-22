<?php

declare(strict_types=1);

/**
 * Empty declarations for class-likes a project references but does not define.
 *
 * Psalm analyses uploaded source without the project's ``vendor/`` (decision:
 * bundled-security-rules-are-redistributable). A trait, interface or parent class
 * that lives in ``vendor/`` is then missing, and Psalm aborts on a missing trait.
 * This script parses the project files with the php-parser bundled with Psalm
 * (no project code is loaded or executed) and writes a stub file declaring every
 * referenced-but-undefined trait, interface and parent class, empty.
 *
 * Usage: php missing-symbols.php <file-list> <stub-output> [<declared-stub> ...]
 *   <file-list>       one absolute PHP file path per line
 *   <declared-stub>   stub files (framework taint models) whose declarations count as defined
 */

use PhpParser\Node;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitor\NameResolver;
use PhpParser\NodeVisitorAbstract;
use PhpParser\ParserFactory;

require __DIR__ . '/vendor/autoload.php';

const MAX_FILE_BYTES = 2_000_000;

if ($argc < 3) {
    fwrite(STDERR, "Usage: php missing-symbols.php <file-list> <stub-output> [<declared-stub> ...]\n");
    exit(2);
}

final class ClassLikeCollector extends NodeVisitorAbstract
{
    /** @var array<string,true> lower-case FQN → declared */
    public array $declared = [];
    /** @var array<string,array{name:string,kind:string}> lower-case FQN → referenced name and kind */
    public array $referenced = [];

    public function enterNode(Node $node): null
    {
        if ($node instanceof Node\Stmt\ClassLike && $node->namespacedName !== null) {
            $this->declared[strtolower($node->namespacedName->toString())] = true;
        }
        if ($node instanceof Node\Stmt\Class_) {
            if ($node->extends !== null) {
                $this->reference($node->extends, 'class');
            }
            foreach ($node->implements as $name) {
                $this->reference($name, 'interface');
            }
        } elseif ($node instanceof Node\Stmt\Interface_) {
            foreach ($node->extends as $name) {
                $this->reference($name, 'interface');
            }
        } elseif ($node instanceof Node\Stmt\Enum_) {
            foreach ($node->implements as $name) {
                $this->reference($name, 'interface');
            }
        } elseif ($node instanceof Node\Stmt\TraitUse) {
            foreach ($node->traits as $name) {
                $this->reference($name, 'trait');
            }
        }
        return null;
    }

    private function reference(Node\Name $name, string $kind): void
    {
        $fqn = ltrim($name->toString(), '\\');
        $this->referenced[strtolower($fqn)] ??= ['name' => $fqn, 'kind' => $kind];
    }
}

$parser = (new ParserFactory())->createForHostVersion();
$collector = new ClassLikeCollector();
$traverser = new NodeTraverser();
$traverser->addVisitor(new NameResolver());
$traverser->addVisitor($collector);

$paths = [...array_slice($argv, 3), ...(file($argv[1], FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) ?: [])];
foreach ($paths as $path) {
    if (!is_file($path) || filesize($path) > MAX_FILE_BYTES) {
        continue;
    }
    try {
        $traverser->traverse($parser->parse((string) file_get_contents($path)) ?? []);
    } catch (\PhpParser\Error) {
        continue;
    }
}

$byNamespace = [];
foreach ($collector->referenced as $key => ['name' => $fqn, 'kind' => $kind]) {
    $builtin = match ($kind) {
        'trait' => trait_exists($fqn, false),
        'interface' => interface_exists($fqn, false),
        default => class_exists($fqn, false),
    };
    if (isset($collector->declared[$key]) || $builtin || !preg_match('/^[A-Za-z_\x80-\xff][\w\x80-\xff\\\\]*$/', $fqn)) {
        continue;
    }
    $position = strrpos($fqn, '\\');
    $namespace = $position === false ? '' : substr($fqn, 0, $position);
    $short = $position === false ? $fqn : substr($fqn, $position + 1);
    $byNamespace[$namespace][] = ($kind === 'class' ? 'abstract class' : $kind) . " {$short} {}";
}

$out = "<?php\n// Generated: class-likes referenced by the project but defined outside it (vendor/).\n";
ksort($byNamespace);
foreach ($byNamespace as $namespace => $declarations) {
    $out .= "namespace {$namespace} {\n    " . implode("\n    ", $declarations) . "\n}\n";
}
file_put_contents($argv[2], $out);
echo array_sum(array_map('count', $byNamespace)), "\n";
