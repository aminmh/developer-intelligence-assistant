<?php

namespace App\Services;

use Illuminate\Support\Facades\File;
use Illuminate\Support\Facades\Storage;
use RuntimeException;
use Smalot\PdfParser\Parser as PdfParser;

class RagUploadExtractorService
{
    // Downloads the finished tusd upload from MinIO, permanently stores it
    // under storage/app/private/rag-uploads/{uploadSessionId}/{filename}, and
    // extracts plain text. Returns ['text' => string, 'type' => string, 'path' => string].
    public function extract(string $objectKey, string $filename, string $uploadSessionId): array
    {
        $ext = strtolower('.' . pathinfo($filename, PATHINFO_EXTENSION));

        $disk = Storage::disk('tus_uploads');
        if (!$disk->exists($objectKey)) {
            throw new RuntimeException("Object not found: {$objectKey}");
        }

        $tempDisk = Storage::build([
            'driver' => 'local',
            'root' => storage_path('app/temp_rag/' . $uploadSessionId),
        ]);
        $tempPath = 'download';

        try {
            $tempDisk->writeStream($tempPath, $disk->readStream($objectKey));

            $storedRelativePath = "rag-uploads/{$uploadSessionId}/{$filename}";
            $storedDisk = Storage::disk('local');
            $storedDisk->makeDirectory("rag-uploads/{$uploadSessionId}");

            // Same filesystem (both under storage/app), so this is a fast
            // atomic move rather than a full re-copy.
            rename($tempDisk->path($tempPath), $storedDisk->path($storedRelativePath));
            $storedFullPath = $storedDisk->path($storedRelativePath);

            if ($ext === '.zip') {
                // Stored above, but contents are not extracted/parsed -- index
                // a placeholder rather than treating the binary archive bytes
                // as text, which would flood chunking/embedding with garbage.
                return [
                    'text' => "Uploaded ZIP archive: {$filename} (binary content, not text-indexed).",
                    'type' => 'zip',
                    'path' => $storedRelativePath,
                ];
            }

            if ($ext === '.pdf') {
                $parser = new PdfParser();
                $document = $parser->parseFile($storedFullPath);

                return ['text' => $document->getText(), 'type' => 'pdf', 'path' => $storedRelativePath];
            }

            $text = file_get_contents($storedFullPath);
            $type = $ext === '.md' ? 'markdown' : 'text';

            return ['text' => $text, 'type' => $type, 'path' => $storedRelativePath];
        } finally {
            File::deleteDirectory($tempDisk->path(''));
        }
    }
}
