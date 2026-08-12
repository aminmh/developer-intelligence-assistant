<?php

namespace App\Http\Controllers;

use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\File;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;

class WorkspaceController extends Controller
{
    // Downloads the project ZIP from MinIO and extracts it to
    // /workspace/project/{workspace_id}. Returns the workspace ID
    // for subsequent MCP tool calls.
    public function prepare(Request $request): JsonResponse
    {
        $filename = $request->input('filename');
        if (!$filename) {
            return response()->json(['error' => 'Missing filename.'], 400);
        }

        $disk = Storage::disk('tus_uploads');
        $baseDir = 'rag-uploads';
        $matches = [];

        if ($disk->exists($baseDir)) {
            foreach ($disk->directories($baseDir) as $sessionDir) {
                $sid = basename($sessionDir);
                $candidatePath = "{$sessionDir}/{$filename}";
                if ($disk->exists($candidatePath)) {
                    $matches[] = ['path' => $candidatePath, 'session_id' => $sid];
                }
            }
        }

        if (empty($matches)) {
            return response()->json(['error' => "File not found: {$filename}"], 404);
        }

        $zipS3Path = $matches[0]['path'];
        $workspaceId = (string) Str::uuid();

        $tmpZip = tempnam(sys_get_temp_dir(), 'ws_zip_');
        file_put_contents($tmpZip, $disk->get($zipS3Path));

        $extractPath = Storage::disk('workspace')->path('/project/'.$workspaceId);
        File::makeDirectory($extractPath, 0755, true, true);

        $zip = new \ZipArchive();
        if ($zip->open($tmpZip) !== true) {
            @unlink($tmpZip);
            return response()->json(['error' => 'Could not open ZIP file.'], 500);
        }

        // ZipSlip protection
        for ($i = 0; $i < $zip->numFiles; $i++) {
            $entryName = $zip->getNameIndex($i);
            if (str_contains($entryName, '..') || str_starts_with($entryName, '/')) {
                $zip->close();
                @unlink($tmpZip);
                return response()->json(['error' => 'Security violation in ZIP paths.'], 400);
            }
        }

        $zip->extractTo($extractPath);
        $zip->close();
        @unlink($tmpZip);

        $fileCount = count(File::allFiles($extractPath));

        Log::channel('parse_index')->info('Workspace prepared', [
            'workspace_id' => $workspaceId,
            'filename' => $filename,
            'files' => $fileCount,
            'extract_to' => $extractPath,
        ]);

        return response()->json([
            'workspace_id' => $workspaceId,
            'extracted_path' => $extractPath,
            'file_count' => $fileCount,
        ]);
    }

    // Cleans up an extracted workspace directory.
    public function destroy(string $workspaceId): JsonResponse
    {
        $path = Storage::disk('workspace')->path('/project/' . $workspaceId);

        if (File::exists($path)) {
            File::deleteDirectory($path);
        }

        return response()->json(['deleted' => true]);
    }
}