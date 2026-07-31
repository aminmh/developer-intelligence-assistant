<?php

use App\Http\Controllers\AstController;
use App\Http\Controllers\RagUploadController;
use Illuminate\Support\Facades\Route;

Route::post('/internal/analyze-ast', [AstController::class, 'analyze'])
     ->middleware('auth.internal'); // Custom middleware to check a shared secret header

Route::post('/rag/upload', [RagUploadController::class, 'uploadFile']);
Route::get('/rag/files', [RagUploadController::class, 'getFiles']);
Route::delete('/rag/files', [RagUploadController::class, 'deleteFile']);
Route::post('/rag/parse-code', [RagUploadController::class, 'parseCode']);
Route::post('/internal/tusd/hooks', [RagUploadController::class, 'tusdHook']);