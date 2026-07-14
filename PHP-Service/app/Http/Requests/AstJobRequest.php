<?php

namespace App\Http\Requests;

use Illuminate\Foundation\Http\FormRequest;

class AstJobRequest extends FormRequest
{
    public function rules(): array
    {
        return [
            'job_id'     => 'required|string',
            'object_key' => 'required|string', // The path in S3/MinIO
        ];
    }
}
