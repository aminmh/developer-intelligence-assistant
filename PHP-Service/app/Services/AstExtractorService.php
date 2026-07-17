<?php

namespace App\Services;

use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Facades\File;
use Illuminate\Support\Str;
use ZipArchive;
use RuntimeException;

class AstExtractorService
{
    public function process(string $objectKey): array
    {
        $tempDisk = Storage::build([
            'driver' => 'local',
            'root'   => storage_path('app/temp_ast/' . Str::random(16)),
        ]);

        $zipLocalPath = 'project.zip';
        $extractPath  = storage_path('app/temp_ast/extracted_' . Str::random(8));

        try {
            // 1. Download from S3/MinIO to Local Temp
            if (!Storage::disk('s3')->exists($objectKey)) {
                throw new RuntimeException("S3 Object not found: {$objectKey}");
            }

            $fileStream = Storage::disk('s3')->readStream($objectKey);
            $tempDisk->writeStream($zipLocalPath, $fileStream);

            $fullZipPath = $tempDisk->path($zipLocalPath);

            // 2. Extract Safely
            $this->extractSafely($fullZipPath, $extractPath);

            // 3. Run AST Extraction (Placeholder for your logic)
            $astData = $this->runAstAnalysis($extractPath);

            return $astData;

        } finally {
            // 4. Cleanup
            File::deleteDirectory(dirname($tempDisk->path($zipLocalPath)));
            File::deleteDirectory($extractPath);
        }
    }

    protected function extractSafely(string $zipPath, string $dest): void
    {
        $zip = new ZipArchive();
        if ($zip->open($zipPath) !== true) {
            throw new RuntimeException("Could not open ZIP file.");
        }

        // Basic Security: Check for ZipSlip (..)
        for ($i = 0; $i < $zip->numFiles; $i++) {
            $filename = $zip->getNameIndex($i);
            if (str_contains($filename, '..') || str_starts_with($filename, '/')) {
                throw new RuntimeException("Security violation in ZIP paths.");
            }
        }

        File::makeDirectory($dest, 0755, true, true);
        $zip->extractTo($dest);
        $zip->close();
    }

    protected function runAstAnalysis(string $path): array
    {
        // This is where you call your PHP parser logic
        // e.g., using nikic/php-parser or your custom AST tool
        return [
            'status' => 'success',
            'files_processed' => count(File::allFiles($path)),
            'timestamp' => now()->toIso8601String(),
        ];
    }
}
