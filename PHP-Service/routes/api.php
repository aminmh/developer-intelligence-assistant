<?php

use App\Http\Controllers\AstController;
use App\Http\Controllers\RagUploadController;
use App\Http\Controllers\ToolController;
use App\Http\Controllers\WorkspaceController;
use Illuminate\Support\Facades\Route;

Route::post('/internal/analyze-ast', [AstController::class, 'analyze'])
     ->middleware('auth.internal'); // Custom middleware to check a shared secret header

Route::post('/rag/upload', [RagUploadController::class, 'uploadFile']);
Route::get('/rag/files', [RagUploadController::class, 'getFiles']);
Route::delete('/rag/files', [RagUploadController::class, 'deleteFile']);
Route::post('/rag/extract', [RagUploadController::class, 'extractFile']);
Route::post('/rag/parse-code', [RagUploadController::class, 'parseCode']);

// Workspace management for MCP tool execution
Route::post('/workspace/prepare', [WorkspaceController::class, 'prepare']);
Route::delete('/workspace/{id}', [WorkspaceController::class, 'destroy']);

// @deprecated — MCP tools replace this. Keep until all tools migrated.
Route::post('/tools/execute', [ToolController::class, 'execute']);

Route::post('/internal/tusd/hooks', [RagUploadController::class, 'tusdHook']);

// Load MCP tool definitions
require __DIR__ . '/mcp.php';