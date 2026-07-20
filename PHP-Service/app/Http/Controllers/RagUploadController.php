<?php

namespace App\Http\Controllers;

use App\Http\Requests\RagUploadSessionRequest;
use App\Services\RagUploadExtractorService;
use Firebase\JWT\JWT;
use Firebase\JWT\Key;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Str;
use OpenApi\Attributes as OA;
use Throwable;

class RagUploadController extends Controller
{
    #[OA\Post(
        path: '/api/rag/upload-sessions',
        summary: 'Create RAG Document Upload Session',
        description: 'Creates a resumable upload session for a RAG knowledge-base document '
            . '(PDF/ZIP/MD/TXT/JS/TS/TSX/JSON). Returns a signed JWT token that the client uses '
            . 'with the TUS resumable upload protocol.',
        tags: ['RAG Upload'],
        requestBody: new OA\RequestBody(
            required: true,
            content: new OA\JsonContent(
                required: ['filename', 'size'],
                properties: [
                    new OA\Property(property: 'filename', type: 'string'),
                    new OA\Property(property: 'size', type: 'integer'),
                ],
            ),
        ),
        responses: [
            new OA\Response(response: 200, description: 'Session created.'),
            new OA\Response(response: 400, description: 'Unsupported file type for RAG upload'),
            new OA\Response(response: 413, description: 'File is too large'),
        ],
    )]
    public function createSession(RagUploadSessionRequest $request): JsonResponse
    {
        $data = $request->validated();

        $ext = strtolower('.' . pathinfo($data['filename'], PATHINFO_EXTENSION));
        if (!in_array($ext, config('rag.allowed_extensions'), true)) {
            return response()->json(['error' => 'Unsupported file type for RAG upload'], 400);
        }

        if ($data['size'] > config('rag.max_upload_size')) {
            return response()->json(['error' => 'File is too large'], 413);
        }

        $uploadSessionId = (string) Str::uuid();
        $expiresAt = now()->addMinutes(60);

        $token = JWT::encode([
            'sub' => 'user-123',
            'upload_session_id' => $uploadSessionId,
            'filename' => $data['filename'],
            'maximum_size' => $data['size'],
            'upload_kind' => 'rag-document',
            'exp' => $expiresAt->timestamp,
        ], config('rag.upload_token_secret'), 'HS256');

        return response()->json([
            'upload_session_id' => $uploadSessionId,
            'tus_endpoint' => config('rag.tus_public_url'),
            'upload_token' => $token,
            'expires_at' => $expiresAt->toIso8601String(),
        ]);
    }

    #[OA\Post(
        path: '/api/internal/tusd/hooks',
        summary: 'TUSD Webhook Handler (RAG documents)',
        description: 'Receives post-finish hook events from tusd after a resumable RAG-document '
            . 'upload completes. Verifies the upload-session JWT carried in the tus metadata '
            . '(tusd itself cannot sign hook deliveries), downloads the object from MinIO, '
            . 'extracts text, and forwards it to the web app\'s RAG indexing callback.',
        tags: ['RAG Upload'],
        responses: [
            new OA\Response(response: 200, description: 'Hook processed (or ignored if not a RAG-document upload).'),
            new OA\Response(response: 401, description: 'Missing, invalid, or mismatched upload token.'),
            new OA\Response(response: 500, description: 'Extraction or callback failed.'),
        ],
    )]
    public function tusdHook(Request $request, RagUploadExtractorService $extractor): JsonResponse
    {
        $payload = $request->json()->all();

        if (($payload['Type'] ?? null) !== 'post-finish') {
            return response()->json(['ok' => true, 'handled' => false]);
        }

        $upload = $payload['Event']['Upload'] ?? [];
        $metadata = $upload['MetaData'] ?? [];

        if (($metadata['upload_kind'] ?? null) !== 'rag-document') {
            // Not ours -- ignore (e.g. a delivery from some other upload kind).
            return response()->json(['ok' => true, 'handled' => false]);
        }

        $token = $metadata['upload_token'] ?? null;
        if (!$token) {
            return response()->json(['error' => 'Missing upload token'], 401);
        }

        try {
            $claims = (array) JWT::decode($token, new Key(config('rag.upload_token_secret'), 'HS256'));
        } catch (Throwable $e) {
            return response()->json(['error' => 'Invalid or expired upload token'], 401);
        }

        if (($claims['upload_kind'] ?? null) !== 'rag-document') {
            return response()->json(['error' => 'Token not valid for RAG upload'], 401);
        }
        if (($claims['upload_session_id'] ?? null) !== ($metadata['upload_session_id'] ?? null)) {
            return response()->json(['error' => 'Upload session mismatch'], 401);
        }

        $storage = $upload['Storage'] ?? [];
        $objectKey = $storage['Key'] ?? null;
        if (!$objectKey) {
            return response()->json(['error' => 'Missing upload storage information'], 400);
        }

        $filename = $claims['filename'];
        $uploadSessionId = $claims['upload_session_id'];

        try {
            $extracted = $extractor->extract($objectKey, $filename, $uploadSessionId);
        } catch (Throwable $e) {
            Log::error('RAG extraction failed', ['error' => $e->getMessage(), 'filename' => $filename]);

            return response()->json(['error' => 'Extraction failed: ' . $e->getMessage()], 500);
        }

        Log::info('RAG upload stored', ['path' => $extracted['path'], 'filename' => $filename]);

        $response = Http::withHeaders([
            'X-Internal-Secret' => config('rag.callback_secret'),
        ])->post(config('rag.node_callback_url'), [
            'name' => $filename,
            'type' => $extracted['type'],
            'size' => $upload['Size'] ?? 0,
            'text' => $extracted['text'],
            'uploadSessionId' => $uploadSessionId,
        ]);

        return response()->json([
            'ok' => true,
            'handled' => true,
            'forwarded_to_node' => true,
            'node_status' => $response->status(),
        ]);
    }
}
