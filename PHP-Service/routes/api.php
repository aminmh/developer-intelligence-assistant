<?php

use App\Http\Controllers\AstController;
use Illuminate\Support\Facades\Route;

Route::post('/internal/analyze-ast', [AstController::class, 'analyze'])
     ->middleware('auth.internal'); // Custom middleware to check a shared secret header