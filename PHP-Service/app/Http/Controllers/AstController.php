<?php

namespace App\Http\Controllers;

use App\Http\Requests\AstJobRequest;
use App\Services\AstExtractorService;
use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\Log;
use OpenApi\Attributes as OA;

class AstController extends Controller
{
    #[OA\Post(
        path: '/api/internal/analyze-ast',
        summary: 'Analyze uploaded PHP codebase',
        description: 'Downloads the ZIP file identified by object_key from S3/MinIO, parses it into an '
            . 'AST-derived code-analysis report (symbols and call graph), and returns the result. '
            . 'Called internally by the Python service after a resumable upload completes.',
        security: [['internalSecret' => []]],
        tags: ['AST Analysis'],
        requestBody: new OA\RequestBody(
            required: true,
            content: new OA\JsonContent(
                required: ['job_id', 'object_key'],
                properties: [
                    new OA\Property(property: 'job_id', type: 'string', description: 'Identifier of the upload/processing job.'),
                    new OA\Property(property: 'object_key', type: 'string', description: 'Path of the ZIP file within the S3/MinIO bucket.'),
                ],
            ),
        ),
        responses: [
            new OA\Response(
                response: 200,
                description: 'Analysis completed successfully.',
                content: new OA\JsonContent(
                    properties: [
                        new OA\Property(property: 'job_id', type: 'string'),
                        new OA\Property(property: 'data', type: 'object', description: 'AST-derived code-analysis report.'),
                    ],
                ),
            ),
            new OA\Response(
                response: 401,
                description: 'Missing or invalid X-Internal-Secret header.',
            ),
            new OA\Response(
                response: 500,
                description: 'Analysis failed.',
                content: new OA\JsonContent(
                    properties: [
                        new OA\Property(property: 'job_id', type: 'string'),
                        new OA\Property(property: 'error', type: 'string'),
                    ],
                ),
            ),
        ],
    )]
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
