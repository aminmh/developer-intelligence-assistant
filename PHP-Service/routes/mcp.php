<?php

use App\MCP\Tools\ChunkCodeTool;
use App\MCP\Tools\ExportStructureTool;
use PhpMcp\Laravel\Facades\Mcp;

Mcp::tool([ChunkCodeTool::class, 'handle'])
    ->name('chunk_code')
    ->description('Parse PHP code into AST-based chunks using the code:parse Artisan command')
    ->inputSchema([
        'type' => 'object',
        'properties' => [
            'workspace_id' => [
                'type' => 'string',
                'description' => 'Pre-extracted workspace directory ID under /workspace/project/',
            ],
            'filename' => [
                'type' => 'string',
                'description' => 'ZIP filename in MinIO (tus-uploads) to parse',
            ],
            'path' => [
                'type' => 'string',
                'description' => 'Relative path inside workspace, or absolute path if no workspace_id',
            ],
            'project' => [
                'type' => 'boolean',
                'default' => false,
            ],
            'export_path' => [
                'type' => 'string',
            ],
            'export_format' => [
                'type' => 'string',
                'enum' => ['json', 'yaml', 'text'],
            ],
            'no_index' => [
                'type' => 'boolean',
                'default' => false,
            ],
            'dry_run' => [
                'type' => 'boolean',
                'default' => false,
            ],
            'batch' => [
                'type' => 'integer',
                'default' => 20,
            ],
        ],
        'additionalProperties' => false,
    ]);

Mcp::tool([ExportStructureTool::class, 'handle'])
    ->name('export_structure')
    ->description('Export a RAG-friendly Markdown project directory structure using tree')
    ->inputSchema([
        'type' => 'object',
        'properties' => [
            'workspace_id' => [
                'type' => 'string',
                'description' => 'Pre-extracted workspace directory ID under /workspace/project/',
            ],
            'filename' => [
                'type' => 'string',
                'description' => 'ZIP filename in MinIO (tus-uploads) — downloaded and extracted automatically',
            ],
            'path' => [
                'type' => 'string',
                'description' => 'Project directory path (default: workspace root)',
            ],
            'depth' => [
                'type' => 'integer',
                'default' => 4,
                'description' => 'Maximum directory depth for the tree output',
            ],
            'include_hidden' => [
                'type' => 'boolean',
                'default' => false,
                'description' => 'Include hidden files and directories',
            ],
        ],
        'additionalProperties' => false,
    ]);
