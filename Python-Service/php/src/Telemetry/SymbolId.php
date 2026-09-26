<?php

declare(strict_types=1);

namespace App\Telemetry;

use InvalidArgumentException;

/** Phase 0 identity contract. No framework or telemetry SDK dependency. */
final class SymbolId
{
    private const IDENTIFIER = '[A-Za-z_][A-Za-z0-9_]*';

    public static function normalize(array $input): string
    {
        return match (self::field($input, 'kind')) {
            'method' => self::qualified(self::field($input, 'class')) . '::' . self::identifier(self::field($input, 'method')),
            'function' => '\\' . self::qualified(self::field($input, 'name')),
            'closure' => self::closure($input),
            'controller' => self::controller(self::field($input, 'controller')),
            'route' => self::route($input),
            'table' => 'table:' . self::identifier(self::field($input, 'connection')) . '.' . self::identifier(self::field($input, 'name')),
            default => throw new InvalidArgumentException('Unsupported symbol kind'),
        };
    }

    /** Unsupported AST declarations intentionally retain only their legacy identity. */
    public static function fromChunk(array $chunk): ?string
    {
        $kind = $chunk['node_type'] ?? $chunk['type'] ?? '';
        if ($kind === 'method') {
            return self::normalize(['kind' => 'method', 'class' => $chunk['class_fqn'] ?? $chunk['parent_symbol'] ?? null,
                'method' => $chunk['method_name'] ?? $chunk['name'] ?? null]);
        }
        if ($kind === 'function') {
            $name = self::field(['name' => $chunk['function_name'] ?? $chunk['name'] ?? null], 'name');
            $namespace = $chunk['namespace'] ?? '';
            if (!is_string($namespace)) throw new InvalidArgumentException('Invalid namespace');
            $namespace = trim($namespace);
            return self::normalize(['kind' => 'function', 'name' => $namespace === '' ? $name : self::qualified($namespace) . '\\' . self::identifier($name)]);
        }
        if ($kind === 'closure' || $kind === 'arrow_function') {
            return self::normalize(['kind' => 'closure', 'path' => $chunk['file_path'] ?? $chunk['file'] ?? null,
                'line' => $chunk['start_line'] ?? null]);
        }
        return null;
    }

    private static function field(array $input, string $key): string
    {
        if (!isset($input[$key]) || !is_string($input[$key]) || trim($input[$key]) === '') {
            throw new InvalidArgumentException('Missing or invalid ' . $key);
        }
        return trim($input[$key]);
    }

    private static function identifier(string $value): string
    {
        if (!preg_match('/\A' . self::IDENTIFIER . '\z/D', $value)) {
            throw new InvalidArgumentException('Invalid identifier');
        }
        return $value;
    }

    private static function qualified(string $value): string
    {
        if (str_starts_with($value, '\\')) $value = substr($value, 1);
        foreach (explode('\\', $value) as $segment) self::identifier($segment);
        return $value;
    }

    private static function closure(array $input): string
    {
        $path = str_replace('\\', '/', self::field($input, 'path'));
        while (str_starts_with($path, './')) $path = substr($path, 2);
        if ($path === '' || str_starts_with($path, '/') || preg_match('/[:\x00-\x1f\x7f]/', $path)) {
            throw new InvalidArgumentException('Invalid repository-relative path');
        }
        foreach (explode('/', $path) as $segment) {
            if ($segment === '' || $segment === '.' || $segment === '..') {
                throw new InvalidArgumentException('Invalid repository-relative path');
            }
        }
        $line = $input['line'] ?? null;
        if (!is_int($line) || $line < 1) throw new InvalidArgumentException('Closure line must be a positive integer');
        return 'closure@' . $path . ':' . $line;
    }

    private static function controller(string $value): string
    {
        $parts = preg_split('/@|::/', $value);
        if (count($parts) > 2) throw new InvalidArgumentException('Invalid controller reference');
        $class = trim($parts[0]);
        if (!str_contains($class, '\\')) throw new InvalidArgumentException('Unresolved controller service identifier');
        return self::qualified($class) . '::' . self::identifier(isset($parts[1]) ? trim($parts[1]) : '__invoke');
    }

    private static function route(array $input): string
    {
        $method = self::field($input, 'method');
        if (!preg_match('/\A[A-Za-z]+\z/D', $method)) throw new InvalidArgumentException('Route requires one HTTP method');
        $template = self::field($input, 'template');
        $withoutParameters = preg_replace('/\{' . self::IDENTIFIER . '\??\}/', 'parameter', $template);
        if (!str_starts_with($template, '/') || preg_match('/[\s\x00-\x1f\x7f?#\\\\]/', $withoutParameters)
            || str_contains($withoutParameters, '{') || str_contains($withoutParameters, '}') || str_contains($template, '//')) {
            throw new InvalidArgumentException('Invalid route template');
        }
        foreach (explode('/', $withoutParameters) as $segment) {
            if ($segment === '.' || $segment === '..') throw new InvalidArgumentException('Invalid route template');
        }
        return 'route:' . strtoupper($method) . ' ' . $template;
    }
}
