<?php

return [

    // Signs/verifies the resumable-upload session JWT (must match the value
    // the browser's tus-js-client metadata carries back on tusd's webhook).
    'upload_token_secret' => env('UPLOAD_TOKEN_SECRET'),

    // Shared secret authenticating the callback into web's Node server.
    'callback_secret' => env('RAG_CALLBACK_SECRET'),

    // Node's POST /api/internal/rag-index endpoint.
    'node_callback_url' => env('NODE_RAG_CALLBACK_URL', 'http://web:3000/api/internal/rag-index'),

    // Browser-facing tusd endpoint, returned in the upload-session response.
    'tus_public_url' => env('TUS_PUBLIC_URL', 'http://localhost:1080/files/'),

    // Browser origin allowed to call /api/rag/upload-sessions directly.
    'web_origin' => env('WEB_ORIGIN', 'http://localhost:3000'),

    'max_upload_size' => (int) env('MAX_RAG_UPLOAD_SIZE', 2 * 1024 * 1024 * 1024), // 2 GiB

    'allowed_extensions' => ['.pdf', '.zip', '.md', '.txt', '.js', '.ts', '.tsx', '.json'],

];
