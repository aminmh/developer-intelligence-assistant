import express from 'express';
import path from 'path';
import { createServer as createViteServer } from 'vite';
import dotenv from 'dotenv';
import { RAGService } from './src/server/rag';
import { UploadedFile } from './src/types';

dotenv.config();

function labelForExtension(filename: string): string {
  const ext = path.extname(filename).toLowerCase();
  if (ext === '.pdf') return 'pdf';
  if (ext === '.zip') return 'zip';
  if (ext === '.md') return 'markdown';
  return 'text';
}

async function startServer() {
  const app = express();
  const PORT = parseInt(process.env.PORT || '3000', 10);

  // Set large JSON limit for uploading larger documents (including base64 PDF strings,
  // and extracted plain text handed back by python-api's large-file TUS pipeline)
  app.use(express.json({ limit: '100mb' }));
  app.use(express.urlencoded({ limit: '100mb', extended: true }));

  // CORS - allow cross-origin requests from any frontend origin
  app.use((req, res, next) => {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, DELETE, PUT, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
    if (req.method === 'OPTIONS') {
      return res.sendStatus(204);
    }
    next();
  });

  // API ROUTES (Must go FIRST)
  
  // Get current configurations
  app.get('/api/config', (req, res) => {
    try {
      const config = RAGService.getConfig();
      res.json({ success: true, config });
    } catch (error: any) {
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Save configurations
  app.post('/api/config', (req, res) => {
    try {
      const updated = RAGService.updateConfig(req.body);
      res.json({ success: true, config: updated });
    } catch (error: any) {
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Get uploaded files list (local + PHP-service MinIO files merged)
  app.get('/api/files', async (req, res) => {
    try {
      const files: UploadedFile[] = [...RAGService.getFiles()];
      const seen = new Set(files.map(f => f.uploadSessionId).filter(Boolean));

      // Fetch files from PHP service (TUS uploads stored in MinIO/rag-uploads)
      const phpUrl = process.env.PHP_SERVICE_URL || 'http://127.0.0.1:9003';      
      try {
        const phpRes = await fetch(`${phpUrl}/api/rag/files`);        
        if (phpRes.ok) {
          const data = await phpRes.json() as { files?: any[] };
          for (const f of data.files ?? []) {
            if (f.upload_session_id && seen.has(f.upload_session_id)) continue;
            seen.add(f.upload_session_id);
            files.push({
              id: f.upload_session_id ?? f.filename,
              name: f.filename,
              type: labelForExtension(f.filename),
              size: f.size ?? 0,
              chunkCount: 0,
              uploadedAt: f.last_modified ?? new Date().toISOString(),
              status: 'ready',
              uploadSessionId: f.upload_session_id ?? undefined,
            });
          }
        }
      } catch (err: any) {
        console.error('[files] PHP service unreachable, using local files only:', err.message);
      }

      res.json({ success: true, files });
    } catch (error: any) {
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Upload a file for indexing — forwards to PHP service for storage + extraction
  app.post('/api/upload', async (req, res) => {
    try {
      const { name, size, content } = req.body;
      if (!name || content === undefined) {
        return res.status(400).json({ success: false, error: 'Missing name or content in payload.' });
      }

      // Forward to PHP service for storage and text extraction
      const phpUrl = process.env.PHP_SERVICE_URL || 'http://127.0.0.1:9003';
      const phpRes = await fetch(`${phpUrl}/api/rag/upload`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: name, content, size: size || 0 }),
      });

      if (!phpRes.ok) {
        const phpErr = await phpRes.json().catch(() => ({ error: phpRes.statusText }));
        return res.status(phpRes.status).json({ success: false, error: phpErr.error || 'Upload to PHP service failed.' });
      }

      const phpData = await phpRes.json();

      // Index extracted text locally
      const file = await RAGService.uploadExtractedText(
        phpData.filename,
        phpData.type,
        phpData.size ?? size ?? 0,
        phpData.text,
        phpData.upload_session_id,
      );

      res.json({ success: true, file });
    } catch (error: any) {
      console.error('File upload error:', error);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Internal callback: index text already extracted upstream (large-file TUS path).
  // Authenticated via a shared secret, not by CORS/browser origin — this is called
  // server-to-server by python-api after it downloads and extracts a tusd upload.
  app.post('/api/internal/rag-index', async (req, res) => {
    const secret = req.header('X-Internal-Secret');
    if (!secret || secret !== process.env.RAG_CALLBACK_SECRET) {
      return res.status(401).json({ success: false, error: 'Unauthorized' });
    }
    try {
      const { name, type, size, text, uploadSessionId } = req.body;
      if (!name || !type || typeof text !== 'string' || !uploadSessionId) {
        return res.status(400).json({ success: false, error: 'Missing name, type, text, or uploadSessionId in payload.' });
      }

      const file = await RAGService.indexExtractedText(name, type, size || 0, text, uploadSessionId);
      res.status(202).json({ success: true, file });
    } catch (error: any) {
      console.error('RAG index callback error:', error);
      res.status(500).json({ success: false, error: error.message });
    }
  });
  // Delete file
  app.delete('/api/files/:id', async (req, res) => {
    try {
      const fileId = req.params.id;

      // Capture filename + session before deletion so we can forward to PHP
      const files = RAGService.getFiles();
      const file = files.find(f => f.id === fileId);
      const fileName = file?.name ?? req.body?.filename ?? null;
      const uploadSessionId = file?.uploadSessionId ?? null;

      const deleted = file ? await RAGService.deleteFile(fileId) : true;

      // Also delete from PHP service (MinIO-backed rag-uploads storage)
      if (fileName) {
        const phpUrl = process.env.PHP_SERVICE_URL || 'http://127.0.0.1:9003';
        try {
          await fetch(`${phpUrl}/api/rag/files`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              filename: fileName,
              upload_session_id: uploadSessionId ?? fileId,
            }),
          });
        } catch {
          console.error('[files] Failed to delete from PHP service');
        }
      }

      if (!deleted) {
        return res.status(404).json({ success: false, error: 'File not found.' });
      }

      res.json({ success: true, message: 'File deleted successfully.' });
    } catch (error: any) {
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Query RAG system (Semantic retrieval + LLM prompt generation)
  app.post('/api/query', async (req, res) => {
    try {
      const { query, limit } = req.body;
      if (!query || query.trim() === '') {
        return res.status(400).json({ success: false, error: 'Query parameter is required.' });
      }

      const response = await RAGService.query(query, limit || 4);
      res.json({ success: true, ...response });
    } catch (error: any) {
      console.error('Query execution error:', error);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Retrieve matching chunks only (Semantic search only)
  app.post('/api/retrieve', async (req, res) => {
    try {
      const { query, limit } = req.body;
      if (!query || query.trim() === '') {
        return res.status(400).json({ success: false, error: 'Query parameter is required.' });
      }

      const matches = await RAGService.retrieve(query, limit || 4);
      res.json({ success: true, matches });
    } catch (error: any) {
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Mount Vite middleware for asset serving in Development
  if (process.env.NODE_ENV !== 'production') {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: 'spa',
    });
    app.use(vite.middlewares);
  } else {
    // Serve production static assets from dist folder
    const distPath = path.join(process.cwd(), 'dist');
    app.use(express.static(distPath));
    app.get('*', (req, res) => {
      res.sendFile(path.join(distPath, 'index.html'));
    });
  }

  app.listen(PORT, '0.0.0.0', () => {
    console.log(`[RAG Server] Running on http://localhost:${PORT}`);
  });
}

startServer();
