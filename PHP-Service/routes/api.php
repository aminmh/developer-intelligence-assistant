<?php

use App\Http\Controllers\AstController;
use App\Http\Controllers\RagUploadController;
use Illuminate\Support\Facades\Route;

Route::post('/internal/analyze-ast', [AstController::class, 'analyze'])
     ->middleware('auth.internal'); // Custom middleware to check a shared secret header

Route::post('/rag/upload-sessions', [RagUploadController::class, 'createSession']);
Route::post('/internal/tusd/hooks', [RagUploadController::class, 'tusdHook']);