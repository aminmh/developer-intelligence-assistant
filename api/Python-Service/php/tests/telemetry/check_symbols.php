<?php

declare(strict_types=1);

use App\Telemetry\SymbolId;

// The runtime-side golden check needs only PHP, not Composer or a telemetry SDK.
require dirname(__DIR__, 2) . '/src/Telemetry/SymbolId.php';

$fixtures = json_decode(file_get_contents(__DIR__ . '/symbol_ids.json'), true, flags: JSON_THROW_ON_ERROR);
$results = [];
foreach ($fixtures as $index => $fixture) {
    try {
        $actual = SymbolId::normalize($fixture['input']);
        if (isset($fixture['error']) || $actual !== $fixture['expected']) {
            throw new RuntimeException('Golden symbol mismatch at fixture ' . $index);
        }
        $results[] = ['symbol_id' => $actual];
    } catch (InvalidArgumentException $error) {
        if (!($fixture['error'] ?? false)) throw new RuntimeException('Unexpected rejection at fixture ' . $index, previous: $error);
        $results[] = ['error' => true];
    }
}
if (in_array('--json', $argv, true)) {
    echo json_encode($results, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n";
} else {
    echo count($fixtures) . " shared symbol fixtures passed\n";
}
