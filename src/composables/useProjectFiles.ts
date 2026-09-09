import { ref, type Ref } from 'vue';
import { uploadViaTus } from '../services/tusUpload';
import { UploadedFile } from '../types';
import { makeRequest } from '../ui/makeRequest';

// Files at or above this size use the resumable TUS upload path instead of the
// inline base64/JSON POST, so a dropped connection doesn't mean starting over.
const UPLOAD_THRESHOLD_BYTES = Number(import.meta.env.VITE_TUS_THRESHOLD_BYTES ?? 10 * 1024 * 1024);

export interface ProjectFilesToast {
  (message: string, isSuccess?: boolean): void;
}

export interface UseProjectFilesOptions {
  triggerToast: ProjectFilesToast;
}

export interface ProjectFileState {
  files: Ref<UploadedFile[]>;
  uploading: Ref<boolean>;
  uploadProgress: Ref<Record<string, number>>;
  parsingFileId: Ref<string | null>;
  indexingProgress: Ref<{ total: number; indexed: number; status: string }>;
  indexedFileIds: Ref<Set<string>>;
  activeFileName: Ref<string | null>;
  fetchFiles: () => Promise<void>;
  pollWhileProcessing: () => Promise<void>;
  processFiles: (fileList: FileList) => Promise<void>;
  deleteFile: (id: string, name: string) => Promise<void>;
  parseFile: (id: string, name: string) => Promise<void>;
  indexFile: (id: string, name: string) => Promise<void>;
  attachZip: (file: File) => Promise<void>;
  clearAttach: () => void;
  selectAttachment: (fileName: string) => void;
}

/**
 * Owns the uploaded-project lifecycle used by both the Files and Chat views.
 *
 * The composable intentionally keeps the existing transport and polling
 * semantics. `App.vue` remains responsible for global toasts and lifecycle
 * startup, while this module owns file/indexing state and mutations.
 */
export function useProjectFiles({ triggerToast }: UseProjectFilesOptions): ProjectFileState {
  const files = ref<UploadedFile[]>([]);
  const uploading = ref(false);
  // Per-file upload progress (0-1) for files going through the TUS path.
  const uploadProgress = ref<Record<string, number>>({});
  const parsingFileId = ref<string | null>(null);
  const indexingProgress = ref({ total: 0, indexed: 0, status: '' });
  const indexedFileIds = ref<Set<string>>(new Set());
  const activeFileName = ref<string | null>(null);

  const fetchFiles = async () => {
    try {
      const res = await makeRequest('/api/files');
      const data = await res.json();
      if (data.success || data.files) {
        files.value = data.files || [];
      }
    } catch (err: any) {
      console.error('Failed to load files:', err);
    }
  };

  // Poll the file list while any file is still being indexed in the background
  // (large-file TUS path), so "Processing..." flips to "Ready"/"Failed"
  // without a manual refresh. Bounded so a stuck backend doesn't poll forever.
  const pollWhileProcessing = async () => {
    for (let attempt = 0; attempt < 60; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 3000));
      await fetchFiles();
      if (!files.value.some((file) => file.status === 'processing')) return;
    }
  };

  const uploadLargeFile = async (file: File) => {
    try {
      await uploadViaTus(file, (progress) => {
        uploadProgress.value = {
          ...uploadProgress.value,
          [file.name]: progress.bytesUploaded / progress.bytesTotal,
        };
      });
      triggerToast(`"${file.name}" uploaded — processing in the background.`);
      await fetchFiles();
      // Preserve the previous fire-and-forget polling behavior for TUS uploads.
      void pollWhileProcessing();
    } catch (err: any) {
      console.error('TUS upload failed for:', file.name, err);
      triggerToast(`Failed to upload ${file.name}: ${err.message}`, false);
    } finally {
      const { [file.name]: _removed, ...rest } = uploadProgress.value;
      uploadProgress.value = rest;
    }
  };

  const processFiles = async (fileList: FileList) => {
    uploading.value = true;
    let successCount = 0;
    let failCount = 0;

    for (let i = 0; i < fileList.length; i++) {
      const file = fileList[i];

      if (file.size >= UPLOAD_THRESHOLD_BYTES) {
        // Large files: resumable direct-to-tusd upload, indexed asynchronously.
        // Don't block the rest of the batch on this one file's upload.
        void uploadLargeFile(file);
        continue;
      }

      try {
        // Always read as base64 — binary-safe for any file type. The backend
        // decides what's supported and how to extract it; the client just
        // ships bytes.
        const content = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onerror = () => reject(new Error('File reading error.'));
          reader.onload = () => resolve((reader.result as string).split(',')[1]);
          reader.readAsDataURL(file);
        });

        const response = await makeRequest('/api/upload', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: file.name,
            size: file.size,
            content,
          }),
        });

        const resultData = await response.json();
        if (resultData.success || resultData.file) {
          successCount++;
        } else {
          console.error('Upload failed for:', file.name, resultData.error);
          triggerToast(`Failed to parse ${file.name}: ${resultData.error}`, false);
          failCount++;
        }
      } catch (err: any) {
        console.error('Error processing file:', file.name, err);
        triggerToast(`Error reading ${file.name}: ${err.message}`, false);
        failCount++;
      }
    }

    if (successCount > 0) {
      triggerToast(`Successfully processed and chunked ${successCount} file(s)!`);
      await fetchFiles();
    }
    // Keep the existing lifecycle semantics: the batch is no longer blocking
    // once all inline uploads have completed, even if TUS uploads still run.
    uploading.value = false;
  };

  const deleteFile = async (id: string, name: string) => {
    try {
      const res = await makeRequest(`/api/files/${id}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: name }),
      });
      const data = await res.json();
      if (data.success || data.message) {
        triggerToast(`File "${name}" deleted.`);
        await fetchFiles();
      } else {
        triggerToast(data.error || 'Failed to delete file', false);
      }
    } catch (err: any) {
      triggerToast(`Error: ${err.message}`, false);
    }
  };

  const parseFile = async (id: string, name: string) => {
    parsingFileId.value = id;
    activeFileName.value = name;
    indexingProgress.value = { total: 0, indexed: 0, status: '' };
    triggerToast(`Parsing "${name}" — extracting PHP code...`, true);
    try {
      const res = await makeRequest('/api/rag/parse-code', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: name }),
      });
      const data = await res.json();
      if (data.already_indexed) {
        triggerToast(`Already indexed — collection "${data.collection_name}" exists in Qdrant.`, true);
        indexedFileIds.value = new Set(indexedFileIds.value).add(id);
        parsingFileId.value = null;
        return;
      }

      if (!data.download_token) {
        triggerToast(data.error || 'Parse failed.', false);
        parsingFileId.value = null;
        return;
      }

      // Poll Python indexing progress directly, preserving the existing
      // browser-to-Python path and cadence.
      const downloadId = data.download_id;
      const pythonUrl = 'http://localhost:9002';
      const poll = setInterval(async () => {
        try {
          const sRes = await fetch(`${pythonUrl}/download-jobs/${downloadId}/status`);
          const status = await sRes.json();
          indexingProgress.value = status;
          if (status.status === 'done') {
            clearInterval(poll);
            triggerToast(`Code indexed — ${status.indexed}/${status.total} chunks in collection ${status.collection_name || ''}`, true);
            indexedFileIds.value = new Set(indexedFileIds.value).add(id);
            await fetchFiles();
            parsingFileId.value = null;
            indexingProgress.value = { total: 0, indexed: 0, status: '' };
          }
        } catch {
          // Python unreachable — keep polling until timeout.
        }
      }, 800);

      // Safety: stop polling after 5 minutes.
      setTimeout(() => {
        clearInterval(poll);
        if (parsingFileId.value) {
          triggerToast('Indexing timed out — check Python service.', false);
          parsingFileId.value = null;
          indexingProgress.value = { total: 0, indexed: 0, status: '' };
        }
      }, 300_000);
    } catch (err: any) {
      triggerToast(`Parse error: ${err.message}`, false);
      parsingFileId.value = null;
    }
  };

  const indexFile = async (id: string, name: string) => {
    // All uploaded files are project ZIP archives now — full parse-code pipeline.
    await parseFile(id, name);
  };

  const attachZip = async (file: File) => {
    try {
      const base64 = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve((reader.result as string).split(',')[1]);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });

      const res = await makeRequest('/api/upload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: file.name, size: file.size, content: base64 }),
      });
      const data = await res.json();
      if (data.success) {
        activeFileName.value = file.name;
        triggerToast(`Project "${file.name}" attached — will be indexed on first query.`, true);
        await fetchFiles();
      } else {
        triggerToast(data.error || 'Failed to attach project.', false);
      }
    } catch (err: any) {
      triggerToast(`Attach error: ${err.message}`, false);
    }
  };

  const clearAttach = () => {
    activeFileName.value = null;
  };

  const selectAttachment = (fileName: string) => {
    activeFileName.value = fileName;
  };

  return {
    files,
    uploading,
    uploadProgress,
    parsingFileId,
    indexingProgress,
    indexedFileIds,
    activeFileName,
    fetchFiles,
    pollWhileProcessing,
    processFiles,
    deleteFile,
    parseFile,
    indexFile,
    attachZip,
    clearAttach,
    selectAttachment,
  };
}
