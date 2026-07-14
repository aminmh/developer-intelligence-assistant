<?php

namespace App\Http\Controllers;

use App\Http\Requests\AstJobRequest;
use App\Services\AstExtractorService;
use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\Log;

class AstController extends Controller
{
    public function analyze(AstJobRequest $request, AstExtractorService $service): JsonResponse
    {
        try {
            $result = $service->process($request->object_key);
            
            return response()->json([
                'job_id' => $request->job_id,
                'data'   => $result
            ]);

        } catch (\Exception $e) {
            Log::error("AST Analysis Failed", ['error' => $e->getMessage()]);
            
            return response()->json([
                'job_id' => $request->job_id,
                'error'  => 'Analysis failed: ' . $e->getMessage()
            ], 500);
        }
    }
}
