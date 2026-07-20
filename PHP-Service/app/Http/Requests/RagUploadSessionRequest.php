<?php

namespace App\Http\Requests;

use Illuminate\Foundation\Http\FormRequest;

class RagUploadSessionRequest extends FormRequest
{
    public function rules(): array
    {
        return [
            'filename' => 'required|string|max:255',
            'size' => 'required|integer|min:1',
        ];
    }
}
