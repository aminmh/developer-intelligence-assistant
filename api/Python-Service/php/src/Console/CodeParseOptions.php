<?php

declare(strict_types=1);

namespace App\Console;

/** Parsed options for the code-parse CLI. */
final class CodeParseOptions
{
    public const OUTPUTS = ['json', 'text', 'summary', 'none'];

    private function __construct(
        public readonly ?string $path,
        public readonly string $output,
        public readonly ?string $typeMapFile,
    ) {
    }

    public static function fromArgv(array $argv): self
    {
        $options = [];
        $args = array_slice($argv, 1);
        for ($i = 0; $i < count($args); $i++) {
            $arg = $args[$i];
            if (str_starts_with($arg, '--') && str_contains($arg, '=')) {
                [$key, $value] = explode('=', $arg, 2);
                $options[substr($key, 2)] = $value;
            } elseif (str_starts_with($arg, '--') && isset($args[$i + 1])) {
                // Support `--path <value>` as well as `--path=<value>`.
                $options[substr($arg, 2)] = $args[++$i];
            }
        }

        return new self(
            isset($options['path']) ? (string) $options['path'] : null,
            isset($options['output']) ? (string) $options['output'] : 'json',
            isset($options['type-map-file']) ? (string) $options['type-map-file'] : null,
        );
    }

    public function hasValidOutput(): bool
    {
        return in_array($this->output, self::OUTPUTS, true);
    }
}
