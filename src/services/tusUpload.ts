import * as tus from 'tus-js-client';

// Base URL of python-api, which mints RAG upload sessions and receives tusd's
// post-finish webhook. Defaults to the host-published dev port.
const UPLOAD_API_URL = (import.meta.env.VITE_UPLOAD_API_URL || 'http://localhost:9002').replace(/\/+$/, '');

export interface TusUploadProgress {
  bytesUploaded: number;
  bytesTotal: number;
}

interface RagUploadSession {
  upload_session_id: string;
  tus_endpoint: string;
  upload_token: string;
  expires_at: string;
}

async function createRagUploadSession(file: File): Promise<RagUploadSession> {
  const res = await fetch(`${UPLOAD_API_URL}/api/rag/upload-sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: file.name, size: file.size }),
  });

  if (!res.ok) {
    const errText = await res.text().catch(() => res.statusText);
    throw new Error(`Failed to create upload session (${res.status}): ${errText}`);
  }

  return res.json();
}

// Uploads a file directly to tusd using the resumable TUS protocol. The upload
// itself is NOT proxied through the Node server — routing it through Node would
// defeat resumability, since the whole point is a direct, interruption-tolerant
// connection between the browser and tusd. Indexing happens asynchronously once
// tusd's post-finish webhook reaches python-api.
export async function uploadViaTus(
  file: File,
  onProgress: (progress: TusUploadProgress) => void
): Promise<{ uploadSessionId: string }> {
  const session = await createRagUploadSession(file);

  return new Promise((resolve, reject) => {
    const upload = new tus.Upload(file, {
      endpoint: session.tus_endpoint,
      retryDelays: [0, 1000, 3000, 5000, 10000],
      chunkSize: 8 * 1024 * 1024,
      removeFingerprintOnSuccess: true,
      headers: {
        Authorization: `Bearer ${session.upload_token}`,
      },
      metadata: {
        filename: file.name,
        filetype: file.type,
        upload_kind: 'rag-document',
        upload_session_id: session.upload_session_id,
        upload_token: session.upload_token,
      },
      onError: (error) => reject(error),
      onProgress: (bytesUploaded, bytesTotal) => onProgress({ bytesUploaded, bytesTotal }),
      onSuccess: () => resolve({ uploadSessionId: session.upload_session_id }),
    });

    upload.findPreviousUploads().then((previousUploads) => {
      if (previousUploads.length > 0) {
        upload.resumeFromPreviousUpload(previousUploads[0]);
      }
      upload.start();
    });
  });
}
