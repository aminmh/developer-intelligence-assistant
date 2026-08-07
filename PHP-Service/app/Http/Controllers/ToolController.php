<?php

namespace App\Http\Controllers;

use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\File;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Storage;
use Symfony\Component\Process\Process;
use Symfony\Component\Yaml\Yaml;
use Throwable;

class ToolController extends Controller
{
    public function execute(Request $request): JsonResponse
    {
        $filename = $request->input('filename');
        $toolNames = $request->input('tool_names', []);

        if (!$filename || empty($toolNames)) {
            return response()->json(['error' => 'Missing filename or tool_names.'], 400);
        }

        $configPath = base_path('config/orchestrator.yml');
        if (!File::exists($configPath)) {
            return response()->json(['error' => 'orchestrator.yml not found'], 500);
        }

        $config = Yaml::parseFile($configPath);
        $toolsConfig = $config['tools'] ?? [];
        $defaults = $config['defaults'] ?? [];
        $projectRoot = $defaults['project_root'] ?? '/workspace/project';
        $maxTimeout = $defaults['max_tool_timeout_seconds'] ?? 120;

        // New-format tools (MCP-backed) — handle without ZIP extraction
        $allNewFormat = true;
        foreach ($toolNames as $tn) {
            if (!isset($toolsConfig[$tn]['server'])) { $allNewFormat = false; break; }
        }

        if ($allNewFormat) {
            $results = [];
            foreach ($toolNames as $tn) {
                $results[] = $this->runNewFormatTool($tn, $toolsConfig[$tn] ?? [], $filename, null);
            }
            Log::channel('parse_index')->info('Tools executed (new format)', [
                'filename' => $filename, 'tools' => $toolNames,
                'results' => array_column($results, 'exit_code', 'tool'),
            ]);
            return response()->json(['results' => $results]);
        }

        // Old-format tools — download ZIP, extract, run
        $extractPath = $this->prepareWorkspace($filename, $projectRoot);
        if (!$extractPath) {
            return response()->json(['results' => [['tool' => 'system', 'stderr' => 'Workspace prep failed', 'exit_code' => -1]]]);
        }

        $results = [];
        foreach ($toolNames as $tn) {
            $tc = $toolsConfig[$tn] ?? null;
            if (!$tc) {
                $results[] = ['tool' => $tn, 'stdout' => '', 'stderr' => "Unknown: {$tn}", 'exit_code' => 1];
                continue;
            }
            if (!($tc['enabled'] ?? true)) {
                $results[] = ['tool' => $tn, 'stdout' => '', 'stderr' => "Disabled: {$tn}", 'exit_code' => 0];
                continue;
            }

            $timeout = $tc['timeout_seconds'] ?? $maxTimeout;
            $cmd = array_merge([$tc['executable'] ?? $tn], $tc['arguments'] ?? []);
            try {
                $p = new Process($cmd, $extractPath);
                $p->setTimeout($timeout);
                $p->run();
                $results[] = ['tool' => $tn, 'stdout' => $p->getOutput(), 'stderr' => $p->getErrorOutput(), 'exit_code' => $p->getExitCode()];
            } catch (Throwable $e) {
                $results[] = ['tool' => $tn, 'stdout' => '', 'stderr' => $e->getMessage(), 'exit_code' => -1];
            }
        }

        File::deleteDirectory($extractPath);
        Log::channel('parse_index')->info('Tools executed', [
            'filename' => $filename, 'tools' => $toolNames,
            'results' => array_column($results, 'exit_code', 'tool'),
        ]);
        return response()->json(['results' => $results]);
    }

    private function prepareWorkspace(string $filename, string $projectRoot): ?string
    {
        $disk = Storage::disk('tus_uploads');
        $files = $disk->allFiles('rag-uploads');
        $zipPath = null;
        foreach ($files as $f) {
            if (basename($f) === $filename) { $zipPath = $f; break; }
        }
        if (!$zipPath) return null;

        $tmpZip = tempnam(sys_get_temp_dir(), 'tool_zip_');
        file_put_contents($tmpZip, $disk->get($zipPath));
        $extractPath = $projectRoot . '/' . pathinfo($filename, PATHINFO_FILENAME);
        File::makeDirectory($extractPath, 0755, true, true);

        $zip = new \ZipArchive();
        if ($zip->open($tmpZip) !== true) { @unlink($tmpZip); return null; }
        for ($i = 0; $i < $zip->numFiles; $i++) {
            $entry = $zip->getNameIndex($i);
            if (str_contains($entry, '..') || str_starts_with($entry, '/')) { $zip->close(); @unlink($tmpZip); return null; }
        }
        $zip->extractTo($extractPath);
        $zip->close();
        @unlink($tmpZip);
        return $extractPath;
    }

    private function runNewFormatTool(string $toolName, array $toolConfig, string $filename, ?string $extractPath): array
    {
        if ($toolName === 'chunk_code') {
            return $this->runChunkCodeTool($filename);
        }

        $commandMap = ['export_structure' => 'project:export-structure'];
        $artisanCmd = $commandMap[$toolName] ?? null;
        if (!$artisanCmd) {
            return ['tool' => $toolName, 'stdout' => '', 'stderr' => "No mapping: {$toolName}", 'exit_code' => 1];
        }

        // Build Artisan args — config args use option names, convert to --kebab-case
        $args = ['--storage-filename' => $filename];
        $configArgs = $toolConfig['args'] ?? [];
        $argMap = ['depth' => '--depth', 'include_hidden' => '--include-hidden'];
        foreach ($configArgs as $key => $val) {
            $option = $argMap[$key] ?? ('--' . str_replace('_', '-', $key));
            $args[$option] = $val;
        }

        try {
            $exitCode = \Illuminate\Support\Facades\Artisan::call($artisanCmd, $args);
            $output = \Illuminate\Support\Facades\Artisan::output();
            return ['tool' => $toolName, 'stdout' => $output, 'stderr' => '', 'exit_code' => $exitCode];
        } catch (Throwable $e) {
            return ['tool' => $toolName, 'stdout' => '', 'stderr' => $e->getMessage(), 'exit_code' => -1];
        }
    }

    private function runChunkCodeTool(string $filename): array
    {
        try {
            $controller = app(\App\Http\Controllers\RagUploadController::class);
            $request = \Illuminate\Http\Request::create('/api/rag/parse-code', 'POST', ['filename' => $filename]);
            $response = $controller->parseCode(
                $request,
                app(\App\Services\AstExtractorService::class),
                app(\App\Services\CodeParser\CodeChunker::class),
                app(\App\Services\CodeParser\ChunkExporter::class),
            );
            $data = json_decode($response->getContent(), true);
            $ok = $data && isset($data['download_token']);
            return ['tool' => 'chunk_code', 'stdout' => json_encode($data), 'stderr' => $ok ? '' : ($data['error'] ?? 'parse failed'), 'exit_code' => $ok ? 0 : 1];
        } catch (Throwable $e) {
            return ['tool' => 'chunk_code', 'stdout' => '', 'stderr' => $e->getMessage(), 'exit_code' => -1];
        }
    }
}