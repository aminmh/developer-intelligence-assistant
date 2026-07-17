<?php

namespace App\OpenApi;

use OpenApi\Attributes as OA;

#[OA\Info(
    version: '1.0.0',
    title: 'PHP AST Analysis Service',
    description: 'Internal Laravel service that parses uploaded PHP codebases into an AST-derived '
        . 'code-analysis report (symbols, call graph) consumed by the Python code-intelligence service.',
)]
#[OA\Server(url: 'http://localhost:9003', description: 'Local development server')]
#[OA\SecurityScheme(
    securityScheme: 'internalSecret',
    type: 'apiKey',
    in: 'header',
    name: 'X-Internal-Secret',
    description: 'Shared secret used by the Python service to authenticate internal-only requests.',
)]
class Annotations
{
}
