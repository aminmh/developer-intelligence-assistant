<?php

declare(strict_types=1);

use App\CodeParser\CodeChunker;
use App\Console\CodeParseRenderer;
use App\Telemetry\SymbolId;

require dirname(__DIR__, 2) . '/vendor/autoload.php';

function check(bool $condition, string $message): void
{
    if (!$condition) throw new RuntimeException($message);
}

$chunker = new CodeChunker();
$chunks = $chunker->chunkFile(__DIR__ . '/fixtures/project.php', 'fixtures/project.php');
$metadata = json_decode((new CodeParseRenderer())->render('json', $chunks), true, flags: JSON_THROW_ON_ERROR);
$ids = array_column($metadata, 'symbol_id');
$expected = [
    '\\App\\Support\\money_format', 'App\\Traits\\Logs::log',
    'App\\Http\\Controllers\\OrderController::store', 'App\\Http\\Controllers\\ShowDashboard::__invoke',
    'closure@fixtures/project.php:24', 'closure@fixtures/project.php:25', '\\global_helper',
];
check($ids === $expected, 'Exporter canonical symbols differ: ' . json_encode($ids));
foreach ($metadata as $item) {
    check(isset($item['chunk_id'], $item['fq_symbol_name']), 'Legacy metadata lost');
    check(!isset($item['symbol_identity_error']), 'Valid fixture rejected');
    if (in_array($item['node_type'], ['class', 'trait'], true)) {
        check(!isset($item['symbol_id']), 'Unsupported declaration must not claim canonical identity');
    }
}
check(!in_array('App\\Http\\Controllers\\OrderController::log', $ids, true), 'Trait consumer identity silently invented');
check(SymbolId::normalize(['kind' => 'controller', 'controller' => 'App\\Http\\Controllers\\ShowDashboard']) === $ids[3], 'Invokable controller mismatch');
check(SymbolId::normalize(['kind' => 'controller', 'controller' => 'App\\Http\\Controllers\\OrderController@store']) === $ids[2], 'Laravel controller mismatch');
check(SymbolId::normalize(['kind' => 'controller', 'controller' => 'App\\Http\\Controllers\\OrderController::store']) === $ids[2], 'Symfony controller mismatch');
check(SymbolId::normalize(['kind' => 'route', 'method' => 'POST', 'template' => '/orders/{order}']) === 'route:POST /orders/{order}', 'Route fixture mismatch');
check(SymbolId::normalize(['kind' => 'table', 'connection' => 'mysql', 'name' => 'orders']) === 'table:mysql.orders', 'Table fixture mismatch');

$invalidPath = $chunker->chunkSource('<?php $f = function () {};', '/tmp/source.php')[0]->toMetadata();
check(!isset($invalidPath['symbol_id']) && isset($invalidPath['symbol_identity_error']), 'Absolute closure path should report identity error');
$anonymous = $chunker->chunkSource('<?php $c = new class { public function run() {} };', 'src/source.php');
check(isset($anonymous[0]->toMetadata()['symbol_identity_error']), 'Anonymous class method should report identity error');
$nested = $chunker->chunkSource('<?php namespace App; class Service { public function run() { function helper() {} $a = fn() => 1; } }', 'src/nested.php');
$nestedIds = array_values(array_filter(array_map(static fn($chunk) => $chunk->toMetadata()['symbol_id'] ?? null, $nested)));
check(in_array('\\App\\helper', $nestedIds, true), 'Nested named function incorrectly became a method');
check(in_array('closure@src/nested.php:1', $nestedIds, true), 'Nested arrow function missing');
echo "Real PHP AST exporter: all six roadmap rows and identity error checks passed\n";
