<?php

declare(strict_types=1);

namespace App\Console;

use PhpParser\Error as ParserError;
use PhpParser\ParserFactory;

/**
 * Pure-PHP syntax validator — the fast fallback used by the Python service when
 * the Phpactor language server is unavailable.
 *
 * The caller is expected to pass a complete PHP file (the Python side wraps
 * code fragments first). Validates with nikic/php-parser (structured line/column
 * diagnostics) and, when the input contains `<?php`, also with `php -l` (the
 * real Zend engine parser) as final authority.
 *
 * Usage:
 *   echo '<?php echo 1;' | php bin/lint-php
 *   php bin/lint-php --file=/path/file.php
 *
 * Outputs JSON: {"valid": bool, "errors": [{"message", "line", "column"}], "php_version": ...}
 * Exit codes: 0 = valid, 1 = invalid, 2 = bad input.
 */
final class LintPhpCommand
{
    public function run(array $argv): int
    {
        [$positional, $opts] = $this->parseArgs($argv);

        if (isset($opts['file'])) {
            $code = @file_get_contents($opts['file']);
            if ($code === false) {
                return $this->emit(2, [['message' => "Could not read file: {$opts['file']}", 'line' => 1, 'column' => 1]]);
            }
        } elseif (!empty($positional)) {
            return $this->emit(2, [['message' => 'Unexpected positional argument', 'line' => 1, 'column' => 1]]);
        } else {
            $code = stream_get_contents(STDIN);
        }

        if ($code === false || trim($code) === '') {
            return $this->emit(2, [['message' => 'No code provided', 'line' => 1, 'column' => 1]]);
        }

        $errors = [];

        // 1. php-parser (structured line/column)
        $parser = (new ParserFactory())->createForNewestSupportedVersion();
        try {
            $parser->parse($code);
        } catch (ParserError $e) {
            $errors[] = [
                'message' => $e->getRawMessage(),
                'line' => max(1, $e->getStartLine()),
                'column' => max(1, $e->getStartColumn($code)),
            ];
        }

        // 2. php -l (authoritative engine parser) when the input is a full file
        if (str_contains($code, '<?php') || str_contains($code, '<?')) {
            $tmp = tempnam(sys_get_temp_dir(), 'lint_php_');
            if ($tmp !== false) {
                try {
                    file_put_contents($tmp, $code);
                    [$exit, , $stderr] = $this->runProcess(['php', '-l', $tmp]);
                    if ($exit !== 0) {
                        $engineError = $this->parsePhpLintError($stderr !== '' ? $stderr : 'syntax error');
                        $already = false;
                        foreach ($errors as $err) {
                            if ($err['message'] === $engineError['message']) {
                                $already = true;
                                break;
                            }
                        }
                        if (!$already) {
                            $errors[] = $engineError;
                        }
                    }
                } finally {
                    @unlink($tmp);
                }
            }
        }

        usort($errors, fn($a, $b) => $a['line'] <=> $b['line']);
        return $this->emit(empty($errors) ? 0 : 1, $errors);
    }

    /**
     * Parse "PHP Parse error:  syntax error, unexpected ... in ... on line N"
     * from `php -l` stderr into {message, line, column}.
     *
     * @return array{message: string, line: int, column: int}
     */
    private function parsePhpLintError(string $stderr): array
    {
        $message = trim($stderr);
        $line = 1;
        if (preg_match('/on line (\d+)/', $message, $m)) {
            $line = max(1, (int) $m[1]);
            $message = trim(preg_replace('/\s+in .* on line \d+$/', '', $message) ?? $message);
        }
        $message = preg_replace('/^(PHP )?(Parse error:?|Fatal error:?)\s*/i', '', $message) ?? $message;
        return ['message' => trim($message), 'line' => $line, 'column' => 1];
    }

    /** @param string[] $cmd @return array{int, string, string} */
    private function runProcess(array $cmd): array
    {
        $descriptors = [
            0 => ['pipe', 'r'],
            1 => ['pipe', 'w'],
            2 => ['pipe', 'w'],
        ];
        $proc = proc_open($cmd, $descriptors, $pipes);
        if (!is_resource($proc)) {
            return [1, '', 'Could not start process'];
        }
        fclose($pipes[0]);
        $stdout = stream_get_contents($pipes[1]);
        $stderr = stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        $exit = proc_close($proc);
        return [$exit, $stdout, $stderr];
    }

    /** @return array{list<string>, array<string, mixed>} */
    private function parseArgs(array $argv): array
    {
        $positional = [];
        $opts = [];
        foreach (array_slice($argv, 1) as $arg) {
            if (str_starts_with($arg, '--') && str_contains($arg, '=')) {
                [$key, $value] = explode('=', $arg, 2);
                $opts[substr($key, 2)] = $value;
            } elseif (str_starts_with($arg, '--')) {
                $opts[substr($arg, 2)] = true;
            } else {
                $positional[] = $arg;
            }
        }
        return [$positional, $opts];
    }

    /**
     * @param array<int, array{message: string, line: int, column: int}> $errors
     */
    private function emit(int $exitCode, array $errors): int
    {
        $payload = [
            'valid' => $exitCode === 0,
            'errors' => $errors,
            'php_version' => PHP_VERSION,
        ];
        echo json_encode($payload, JSON_UNESCAPED_SLASHES);
        echo "\n";
        return $exitCode;
    }
}
