<?php

namespace App\Services;

use Illuminate\Support\Facades\File;
use Illuminate\Support\Facades\Storage;
use RuntimeException;
use Smalot\PdfParser\Parser as PdfParser;

class RagUploadExtractorService
{
    private const STORAGE_DISK = 'tus_uploads';
    private const STORAGE_PREFIX = 'rag-uploads';

    // Downloads the finished tusd upload from MinIO, extracts plain text.
    // File stays in the tus_uploads bucket — no local copy is made.
    // Returns ['text' => string, 'type' => string, 'path' => string].
    public function extract(string $objectKey, string $filename, string $uploadSessionId): array
    {
        $disk = Storage::disk(self::STORAGE_DISK);
        if (!$disk->exists($objectKey)) {
            throw new RuntimeException("Object not found: {$objectKey}");
        }

        $tempPath = tempnam(sys_get_temp_dir(), 'rag_extract_');
        try {
            $tempDisk = Storage::build([
                'driver' => 'local',
                'root' => sys_get_temp_dir(),
            ]);
            $tempDisk->writeStream(basename($tempPath), $disk->readStream($objectKey));

            // Copy to a permanent key under rag-uploads prefix
            $storedKey = self::STORAGE_PREFIX . '/' . $uploadSessionId . '/' . $filename;
            $disk->put($storedKey, file_get_contents($tempPath));

            return $this->extractFromFile($tempPath, $filename, $uploadSessionId);

        } finally {
            if (file_exists($tempPath)) {
                @unlink($tempPath);
            }
        }
    }

    // Accepts raw decoded bytes, stores permanently in MinIO under
    // rag-uploads/{uploadSessionId}/{filename}, and extracts plain text.
    // Returns ['text' => string, 'type' => string, 'path' => string].
    public function storeAndExtract(string $content, string $filename, string $uploadSessionId): array
    {
        $storedKey = self::STORAGE_PREFIX . '/' . $uploadSessionId . '/' . $filename;

        $disk = Storage::disk(self::STORAGE_DISK);
        $disk->put($storedKey, $content);

        $tmpPath = tempnam(sys_get_temp_dir(), 'rag_store_');
        file_put_contents($tmpPath, $content);
        try {
            return $this->extractFromFile($tmpPath, $filename, $uploadSessionId);
        } finally {
            if (file_exists($tmpPath)) {
                @unlink($tmpPath);
            }
        }
    }

    private function extractFromFile(string $filePath, string $filename, string $uploadSessionId): array
    {
        $ext = strtolower('.' . pathinfo($filename, PATHINFO_EXTENSION));
        $path = self::STORAGE_PREFIX . '/' . $uploadSessionId . '/' . $filename;

        if ($ext === '.zip') {
            return [
                'text' => "Uploaded ZIP archive: {$filename} (binary content, not text-indexed).",
                'type' => 'zip',
                'path' => $path,
            ];
        }

        if ($ext === '.pdf') {
            $parser = new PdfParser();
            $document = $parser->parseFile($filePath);

            return ['text' => $document->getText(), 'type' => 'pdf', 'path' => $path];
        }

        $text = file_get_contents($filePath);
        $type = $ext === '.md' ? 'markdown' : 'text';

        return ['text' => $text, 'type' => $type, 'path' => $path];
    }
}