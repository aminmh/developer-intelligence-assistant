<?php

namespace App\Http\Controllers;

use App\Http\Requests\RagUploadSessionRequest;
use App\Services\AstExtractorService;
use App\Services\CodeParser\ChunkExporter;
use App\Services\CodeParser\CodeChunker;
use App\Services\RagUploadExtractorService;
use Firebase\JWT\JWT;
use Firebase\JWT\Key;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\File;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Storage;
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

    #[OA\Get(
        path: '/api/rag/files',
        summary: 'List uploaded RAG files',
        description: 'Returns a list of all uploaded RAG documents stored on the server.',
        tags: ['RAG Upload'],
        responses: [
            new OA\Response(response: 200, description: 'List of uploaded files.'),
        ],
    )]
    public function getFiles(): JsonResponse
    {
        $disk = Storage::disk('local');
        $baseDir = 'rag-uploads';

        $files = [];

        if (!$disk->exists($baseDir)) {
            return response()->json(['files' => []]);
        }

        foreach ($disk->directories($baseDir) as $sessionDir) {
            $uploadSessionId = basename($sessionDir);

            foreach ($disk->files($sessionDir) as $filePath) {
                $filename = basename($filePath);

                $files[] = [
                    'filename' => $filename,
                    'path' => $filePath,
                    'size' => $disk->size($filePath),
                    'last_modified' => date('c', $disk->lastModified($filePath)),
                    'upload_session_id' => $uploadSessionId,
                ];
            }
        }

        return response()->json(['files' => $files]);
    }

    #[OA\Delete(
        path: '/api/rag/files',
        summary: 'Delete an uploaded RAG file',
        description: 'Deletes a file from the rag-uploads storage by filename and optional session ID. '
            . 'If multiple copies exist and no session ID is given, the most recently modified one is removed.',
        tags: ['RAG Upload'],
        requestBody: new OA\RequestBody(
            required: true,
            content: new OA\JsonContent(
                required: ['filename'],
                properties: [
                    new OA\Property(property: 'filename', type: 'string', description: 'The file to delete'),
                    new OA\Property(property: 'upload_session_id', type: 'string', description: 'Optional session ID to disambiguate'),
                ],
            ),
        ),
        responses: [
            new OA\Response(response: 200, description: 'File deleted'),
            new OA\Response(response: 404, description: 'File not found'),
        ],
    )]
    public function deleteFile(Request $request): JsonResponse
    {
        $filename = $request->input('filename');
        if (!$filename) {
            return response()->json(['error' => 'Missing filename.'], 400);
        }

        $uploadSessionId = $request->input('upload_session_id');
        $disk = Storage::disk('local');
        $baseDir = 'rag-uploads';
        $matches = [];

        if ($disk->exists($baseDir)) {
            foreach ($disk->directories($baseDir) as $sessionDir) {
                $sid = basename($sessionDir);
                $candidatePath = "{$sessionDir}/{$filename}";

                if ($disk->exists($candidatePath)) {
                    $matches[] = [
                        'path' => $candidatePath,
                        'session_id' => $sid,
                        'modified' => $disk->lastModified($candidatePath),
                    ];
                }
            }
        }

        if (empty($matches)) {
            return response()->json(['error' => "File not found: {$filename}"], 404);
        }

        $match = null;
        if ($uploadSessionId) {
            foreach ($matches as $m) {
                if ($m['session_id'] === $uploadSessionId) {
                    $match = $m;
                    break;
                }
            }
            if (!$match) {
                return response()->json(['error' => "File not found for session: {$uploadSessionId}"], 404);
            }
        } else {
            usort($matches, fn($a, $b) => $b['modified'] <=> $a['modified']);
            $match = $matches[0];
        }

        $disk->delete($match['path']);

        // Remove session directory if empty
        $sessionDir = dirname($match['path']);
        if (empty($disk->files($sessionDir))) {
            $disk->deleteDirectory($sessionDir);
        }

        return response()->json([
            'deleted' => true,
            'filename' => $filename,
            'upload_session_id' => $match['session_id'],
        ]);
    }

    #[OA\Post(
        path: '/api/rag/parse-code',
        summary: 'Parse PHP code from uploaded ZIP',
        description: 'Finds an uploaded ZIP file in rag-uploads, extracts it, parses PHP code, '
            . 'and forwards the result to the Python service for download. Returns a download token '
            . 'and URL for retrieving the parsed embeddable text file.',
        tags: ['RAG Upload'],
        requestBody: new OA\RequestBody(
            required: true,
            content: new OA\JsonContent(
                required: ['filename'],
                properties: [
                    new OA\Property(property: 'filename', type: 'string', description: 'The uploaded ZIP filename'),
                    new OA\Property(property: 'upload_session_id', type: 'string', description: 'Optional session ID to disambiguate'),
                ],
            ),
        ),
        responses: [
            new OA\Response(response: 200, description: 'Download token and URL returned'),
            new OA\Response(response: 404, description: 'File not found'),
            new OA\Response(response: 400, description: 'File is not a ZIP or missing filename'),
            new OA\Response(response: 500, description: 'Failed to create download job'),
        ],
    )]
    public function parseCode(
        Request $request,
        AstExtractorService $astExtractor,
        CodeChunker $chunker,
        ChunkExporter $exporter,
    ): JsonResponse {
        $filename = $request->input('filename');
        if (!$filename) {
            return response()->json(['error' => 'Missing filename.'], 400);
        }

        $uploadSessionId = $request->input('upload_session_id');
        $extension = strtolower(pathinfo($filename, PATHINFO_EXTENSION));

        if ($extension !== 'zip') {
            return response()->json(['error' => 'Only ZIP files can be parsed. Got: ' . $extension], 400);
        }

        // Find the file in rag-uploads
        $disk = Storage::disk('local');
        $baseDir = 'rag-uploads';
        $matches = [];

        if ($disk->exists($baseDir)) {
            foreach ($disk->directories($baseDir) as $sessionDir) {
                $sid = basename($sessionDir);
                $candidatePath = "{$sessionDir}/{$filename}";

                if ($disk->exists($candidatePath)) {
                    $matches[] = [
                        'path' => $candidatePath,
                        'session_id' => $sid,
                        'modified' => $disk->lastModified($candidatePath),
                    ];
                }
            }
        }

        if (empty($matches)) {
            return response()->json(['error' => "File not found in uploads: {$filename}"], 404);
        }

        // Prefer exact session match, otherwise most recent
        $match = null;
        if ($uploadSessionId) {
            foreach ($matches as $m) {
                if ($m['session_id'] === $uploadSessionId) {
                    $match = $m;
                    break;
                }
            }
            if (!$match) {
                return response()->json(['error' => "File not found for session: {$uploadSessionId}"], 404);
            }
        } else {
            usort($matches, fn($a, $b) => $b['modified'] <=> $a['modified']);
            $match = $matches[0];
        }

        $zipFullPath = $disk->path($match['path']);

        // Extract to temp directory
        $extractPath = storage_path('app/temp_ast/parse_' . Str::random(16));

        try {
            File::makeDirectory(dirname($extractPath), 0755, true, true);
            $astExtractor->extractSafely($zipFullPath, $extractPath);

            // Collect all chunks
            $allChunks = [];
            foreach ($chunker->chunkDirectory($extractPath) as $chunk) {
                $allChunks[] = $chunk;
            }

            $text = $exporter->toTextString($allChunks);
            $outputFilename = pathinfo($filename, PATHINFO_FILENAME) . '.txt';

            $response = Http::withHeaders([
                'X-Internal-Secret' => config('rag.callback_secret'),
            ])->post('http://python-api:9002/download-jobs', [
                'filename' => $outputFilename,
                'text' => $text,
            ]);

            if (!$response->successful()) {
                Log::error('Python download-job creation failed', [
                    'status' => $response->status(),
                    'body' => $response->body(),
                ]);

                return response()->json(['error' => 'Failed to create download job.'], 500);
            }

            $data = $response->json();

            return response()->json([
                'download_token' => $data['download_token'] ?? null,
                'download_url'   => $data['download_url'] ?? ("http://localhost:9002/download/{$data['download_token']}"),
                'expires_at'     => $data['expires_at'] ?? null,
            ]);
        } finally {
            File::deleteDirectory($extractPath);
        }
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
