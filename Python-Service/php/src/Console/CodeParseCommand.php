<?php

declare(strict_types=1);

namespace App\Console;

/**
 * Pure-PHP replacement for the former Laravel `code:parse` artisan command.
 *
 * Parses PHP files into AST chunks and prints them to stdout.
 *
 * Usage:
 *   php bin/code-parse --path=/abs/project
 *   php bin/code-parse --path=/abs/project --output=json
 *   php bin/code-parse --path=/abs/file.php --output=text
 *
 * Exit codes: 0 = ok, 1 = nothing to scan / bad args, 2 = partial parse failure.
 */
final class CodeParseCommand
{
    private const USAGE = 'Usage: php bin/code-parse --path=<path> [--output=json|text|summary|none] [--type-map-file=FILE]';

    public function run(array $argv): int
    {
        $options = CodeParseOptions::fromArgv($argv);
        if ($options->path === null || trim($options->path) === '') {
            $this->error('Error: --path=<dir-or-file> is required.');
            $this->error(self::USAGE);
            return 1;
        }

        $input = CodeParseInput::resolve($options->path);
        if ($input === null) {
            $this->error("Error: path does not exist: {$options->path}");
            return 1;
        }

        if (!$options->hasValidOutput()) {
            $this->error("Error: --output must be json, text, summary, or none (got: {$options->output}).");
            return 1;
        }

        $typeMapResult = $input->loadTypeMap($options->typeMapFile);
        if ($typeMapResult['warning'] !== null) {
            $this->error($typeMapResult['warning']);
        }

        $scan = (new CodeParseScanner(new \App\CodeParser\CodeChunker()))->scan($input, $typeMapResult['map']);
        $chunks = $scan['chunks'];
        $report = $scan['report'];

        if (empty($chunks)) {
            if ($options->output === 'json') {
                echo "[]\n";
            }
            $this->error("No chunks produced for: {$input->path}");
            return 1;
        }

        echo (new CodeParseRenderer())->render($options->output, $chunks);

        if ($report->hasErrors()) {
            $this->error(count($report->errors()) . ' file(s) had parse errors:');
            foreach ($report->errors() as $error) {
                $this->error("  {$error['file']}: {$error['message']}");
            }
            return 2;
        }

        return 0;
    }

    private function error(string $message): void
    {
        fwrite(STDERR, $message . "\n");
    }
}
