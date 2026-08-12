<?php

declare(strict_types=1);

namespace App\Services\CodeParser;

use Illuminate\Support\Facades\File;
use PhpParser\Node;
use PhpParser\Node\Name;
use PhpParser\Node\Stmt\Class_;
use PhpParser\Node\Stmt\ClassMethod;
use PhpParser\Node\Stmt\Property;
use PhpParser\NodeFinder;
use PhpParser\ParserFactory;

/**
 * Scans workspace PHP files to build a type map for call resolution.
 *
 * Extracts property types from:
 *   - Typed property declarations
 *   - @var docblock annotations
 *   - @property docblock annotations (Eloquent magic properties)
 *   - Relationship method return types (belongsTo, hasMany, etc.)
 *   - Method return types
 *
 * Returns a map:
 *   [ FullyQualifiedClassName => [
 *       "properties" => [ propName => FQCN ],
 *       "methods"    => [ methodName => returnFQCN ],
 *     ]
 *   ]
 */
final class WorkspaceTypeScanner
{
    /**
     * @return array<string, array{properties: array<string,string>, methods: array<string,string|null>}>
     */
    public function scan(string $workspacePath): array
    {
        $map = [];
        $files = $this->findPhpFiles($workspacePath);

        foreach ($files as $filePath) {
            $source = file_get_contents($filePath);
            if ($source === false) continue;

            $parser = (new ParserFactory())->createForNewestSupportedVersion();
            try {
                $ast = $parser->parse($source);
            } catch (\Throwable) {
                continue;
            }
            if ($ast === null) continue;

            $namespace = $this->findNamespace($ast);
            $finder = new NodeFinder();

            foreach ($finder->findInstanceOf($ast, Class_::class) as $class) {
                $className = $class->name?->toString();
                if (!$className) continue;

                $fqcn = $namespace ? $namespace . '\\' . $className : $className;
                $entry = ['properties' => [], 'methods' => []];

                // Scan class docblock for @property annotations
                $classDoc = $class->getDocComment()?->getText() ?? '';
                foreach ($this->parsePropertyAnnotations($classDoc) as $propName => $propType) {
                    $entry['properties'][$propName] = $propType;
                }

                // Scan property declarations + @var docblocks
                foreach ($finder->findInstanceOf($class->stmts, Property::class) as $prop) {
                    $type = $this->resolvePropertyType($prop, $namespace, $ast);
                    if ($type !== null) {
                        foreach ($prop->props as $p) {
                            $entry['properties'][$p->name->toString()] = $type;
                        }
                    }
                }

                // Scan methods for return types + relationship hints
                foreach ($finder->findInstanceOf($class->stmts, ClassMethod::class) as $method) {
                    $methodName = $method->name->toString();
                    $returnType = $this->resolveReturnType($method, $namespace, $ast);
                    $entry['methods'][$methodName] = $returnType;

                    // If this is a relationship method, extract the related model
                    $relatedModel = $this->extractRelationshipModel($method);
                    if ($relatedModel !== null) {
                        $entry['properties'][$methodName] = $relatedModel;
                    } elseif ($returnType !== null && !in_array($returnType, ['int', 'string', 'bool', 'float', 'array', 'void', 'mixed', 'null', 'callable', 'iterable', 'object', 'self', 'static'], true)) {
                        // Method returns a model/class type — likely an accessor
                        // e.g. public function owner(): User { return User::find(...); }
                        // → $this->owner resolves to User
                        $entry['properties'][$methodName] = $returnType;
                    }
                }

                $map[$fqcn] = $entry;
            }
        }

        return $map;
    }

    private function findNamespace(array $ast): ?string
    {
        foreach ($ast as $node) {
            if ($node instanceof Node\Stmt\Namespace_) {
                return $node->name?->toString();
            }
        }
        return null;
    }

    /**
     * @return array<string, string>
     */
    private function parsePropertyAnnotations(string $docblock): array
    {
        $props = [];
        if (preg_match_all('/@property\s+(\S+)\s+\$(\w+)/', $docblock, $matches, PREG_SET_ORDER)) {
            foreach ($matches as $m) {
                $props[$m[2]] = $m[1]; // e.g. "age" => "App\Models\SkiSchool\Age"
            }
        }
        return $props;
    }

    private function resolvePropertyType(Property $prop, ?string $namespace, array $ast): ?string
    {
        // Typed property
        if ($prop->type !== null) {
            return $this->resolveTypeName($prop->type, $namespace, $ast);
        }

        // @var docblock
        $doc = $prop->getDocComment()?->getText() ?? '';
        if (preg_match('/@var\s+(\?)?(\S+)/', $doc, $matches)) {
            $typeName = $matches[2];
            // Remove [] for arrays
            $typeName = rtrim($typeName, '[]');
            return $this->resolveTypeName(new Name($typeName), $namespace, $ast);
        }

        return null;
    }

    private function resolveReturnType(ClassMethod $method, ?string $namespace, array $ast): ?string
    {
        if ($method->returnType !== null) {
            return $this->resolveTypeName($method->returnType, $namespace, $ast);
        }

        // @return docblock
        $doc = $method->getDocComment()?->getText() ?? '';
        if (preg_match('/@return\s+(\?)?(\S+)/', $doc, $matches)) {
            $typeName = $matches[2];
            $typeName = rtrim($typeName, '[]');
            return $this->resolveTypeName(new Name($typeName), $namespace, $ast);
        }

        return null;
    }

    private function resolveTypeName(Node $type, ?string $namespace, array $ast): ?string
    {
        if ($type instanceof Name) {
            $name = $type->toString();

            // Already fully qualified
            if (str_starts_with($name, '\\')) {
                return substr($name, 1);
            }

            // Check use statements
            $useMap = $this->buildUseMap($ast);
            if (isset($useMap[$name])) {
                return $useMap[$name];
            }

            // Same namespace
            if ($namespace && !str_contains($name, '\\')) {
                return $namespace . '\\' . $name;
            }

            return $name;
        }

        if ($type instanceof Node\NullableType) {
            return $this->resolveTypeName($type->type, $namespace, $ast);
        }

        return null;
    }

    /**
     * @return array<string, string> alias → FQCN
     */
    private function buildUseMap(array $ast): array
    {
        $map = [];
        $finder = new NodeFinder();

        foreach ($finder->findInstanceOf($ast, Node\Stmt\Use_::class) as $use) {
            foreach ($use->uses as $u) {
                $fqcn = $u->name->toString();
                $alias = $u->alias?->toString() ?? $u->name->getLast();
                $map[$alias] = $fqcn;
            }
        }

        foreach ($finder->findInstanceOf($ast, Node\Stmt\GroupUse::class) as $group) {
            $prefix = $group->prefix->toString();
            foreach ($group->uses as $u) {
                $fqcn = $prefix . '\\' . $u->name->toString();
                $alias = $u->alias?->toString() ?? $u->name->getLast();
                $map[$alias] = $fqcn;
            }
        }

        return $map;
    }

    /**
     * Extract the related model from a Laravel relationship method.
     *
     *   public function age() { return $this->belongsTo(Age::class); }
     *   → "App\Models\SkiSchool\Age"
     */
    private function extractRelationshipModel(ClassMethod $method): ?string
    {
        $body = $method->stmts ?? [];
        if (count($body) !== 1) return null;

        $stmt = $body[0];
        if (!$stmt instanceof Node\Stmt\Return_) return null;

        $expr = $stmt->expr;
        if (!$expr instanceof Node\Expr\MethodCall) return null;
        if (!$expr->name instanceof Node\Identifier) return null;

        $relationMethod = $expr->name->toString();
        if (!in_array($relationMethod, ['belongsTo', 'hasOne', 'hasMany', 'belongsToMany', 'morphTo', 'morphMany', 'morphToMany', 'morphedByMany', 'hasManyThrough', 'hasOneThrough', 'morphOne'], true)) {
            return null;
        }

        // Get the first argument (the related class)
        $args = $expr->args ?? [];
        if (empty($args)) return null;

        $arg = $args[0]->value;
        if ($arg instanceof Node\Expr\ClassConstFetch && $arg->class instanceof Name) {
            return $arg->class->toString(); // Age::class → "App\Models\SkiSchool\Age"
        }

        return null;
    }

    /**
     * @return string[]
     */
    private function findPhpFiles(string $directory): array
    {
        $files = [];
        $iterator = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($directory, \FilesystemIterator::SKIP_DOTS)
        );
        foreach ($iterator as $file) {
            if ($file->getExtension() === 'php') {
                $files[] = $file->getPathname();
            }
        }
        sort($files);
        return $files;
    }
}
