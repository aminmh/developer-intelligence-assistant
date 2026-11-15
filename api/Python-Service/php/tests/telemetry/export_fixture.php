<?php

declare(strict_types=1);

use App\CodeParser\CodeChunker;
use App\Console\CodeParseRenderer;
use App\Telemetry\SymbolId;

require dirname(__DIR__, 2) . '/vendor/autoload.php';

$chunks = (new CodeChunker())->chunkFile(__DIR__ . '/fixtures/project.php', 'fixtures/project.php');
$metadata = json_decode((new CodeParseRenderer())->render('json', $chunks), true, flags: JSON_THROW_ON_ERROR);

// Framework-derived facts are explicit fixtures, not inferred by the PHP AST parser.
$facts = [
    ['kind' => 'route', 'method' => 'POST', 'template' => '/orders/{order}'],
    ['kind' => 'table', 'connection' => 'mysql', 'name' => 'orders'],
];
echo json_encode(['chunks' => $metadata, 'facts' => array_map(static fn(array $fact): array => [
    'input' => $fact, 'symbol_id' => SymbolId::normalize($fact),
], $facts)], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n";
