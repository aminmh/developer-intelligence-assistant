<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { 
  FileText, 
  Upload, 
  Trash2, 
  FileCode, 
  Cpu,
  CheckCircle2,
} from 'lucide-vue-next';
import { UploadedFile } from '../types';
import type { RuntimeProjectRecord } from '../types';
import { makeRequest } from '../ui/makeRequest';
import ConfirmModal from './ConfirmModal.vue';

const props = defineProps<{
  files: UploadedFile[];
  uploading: boolean;
  uploadProgress: Record<string, number>;
  parsingFileId: string | null;
  indexingProgress: { total: number; indexed: number; status: string; stage?: string; error?: string; graph_status?: string; graph_index_complete?: boolean; graph_nodes?: number; graph_relations?: number };
  indexedFileIds: Set<string>;
  embeddingProvider: 'ollama' | 'openAICompatible' | 'cohere';
}>();

const emit = defineEmits<{
  (e: 'uploadFiles', files: FileList): void;
  (e: 'deleteFile', id: string, name: string): void;
  (e: 'indexFile', id: string, name: string): void;
  (e: 'rebuildCohereEmbeddings', id: string, name: string): void;
}>();

const dragOver = ref(false);
const showDeleteConfirm = ref(false);
const selectedFile = ref<UploadedFile | null>(null);
const activeFileId = ref<string | null>(null);
const runtimeProjects = ref<RuntimeProjectRecord[]>([]);
const runtimeLoadError = ref('');
const progressStageMessage: Record<string, string> = {
  queued: 'Queueing Cohere rebuild...',
  extracting: 'Preparing source revision...',
  embedding: 'Embedding project chunks...',
  graph_indexing: 'Checking Neo4j and writing the project graph...',
  upserting: 'Saving embeddings...',
  activating: 'Activating new embeddings...',
};
const deploymentByScope = computed(() => new Map(runtimeProjects.value
  .filter((row) => row.commit_sha)
  .map((row) => [`${row.project_id}:${row.commit_sha}`, row])));
const runtimeFor = (file: UploadedFile) => file.projectId && file.commitSha
  ? deploymentByScope.value.get(`${file.projectId}:${file.commitSha}`) || null
  : null;
const loadRuntimeProjects = async () => {
  try {
    const response = await makeRequest('/api/runtime/projects');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Runtime deployment status is unavailable.');
    runtimeProjects.value = data.projects || [];
  } catch (cause: any) {
    runtimeLoadError.value = cause?.message || 'Runtime deployment status is unavailable.';
  }
};
onMounted(loadRuntimeProjects);

const handleFileUpload = (event: Event) => {
  const target = event.target as HTMLInputElement;
  if (target.files && target.files.length > 0) {
    emit('uploadFiles', target.files);
    target.value = '';
  }
};

const handleDrop = (event: DragEvent) => {
  dragOver.value = false;
  if (event.dataTransfer?.files && event.dataTransfer.files.length > 0) {
    emit('uploadFiles', event.dataTransfer.files);
  }
};

const triggerUpload = () => {
  document.getElementById('file-upload-input')?.click();
};

const confirmDelete = (file: UploadedFile) => {
  selectedFile.value = file;
  showDeleteConfirm.value = true;
};

const handleDeleteConfirm = () => {
  if (selectedFile.value) {
    emit('deleteFile', selectedFile.value.id, selectedFile.value.name);
  }
  showDeleteConfirm.value = false;
  selectedFile.value = null;
};

const handleDeleteCancel = () => {
  showDeleteConfirm.value = false;
  selectedFile.value = null;
};

const toggleFileOptions = (file: UploadedFile, event: MouseEvent) => {
  // Don't toggle on delete button clicks (handled by stop propagation)
  const target = event.target as HTMLElement;
  if (target.closest('button')) return;

  activeFileId.value = activeFileId.value === file.id ? null : file.id;
};

const handleIndexFile = () => {
  const file = props.files.find(f => f.id === activeFileId.value);
  if (file) {
    emit('indexFile', file.id, file.name);
  }
};
</script>

<template>
  <div class="flex-1 overflow-y-auto p-6 space-y-6 text-left">
    <div class="max-w-4xl mx-auto space-y-6">
      
      <!-- Upload zone card -->
      <div 
        class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6"
        @dragover.prevent="dragOver = true"
        @dragleave="dragOver = false"
        @drop="handleDrop"
        :class="dragOver ? 'border-indigo-500 bg-indigo-500/5' : ''"
      >
        <div class="flex items-center justify-between mb-4">
          <h4 class="text-xs font-bold tracking-wider uppercase text-slate-400 flex items-center gap-2">
            <Upload class="w-4 h-4 text-indigo-400" />
            Upload Project Archives
          </h4>
          <span class="text-[10px] text-slate-500">Accepted: ZIP</span>
        </div>

        <div
          @click="triggerUpload"
          class="border-2 border-dashed border-slate-800 rounded-xl p-8 text-center cursor-pointer hover:border-indigo-500/50 transition-all"
        >
          <input
            id="file-upload-input"
            type="file"
            multiple
            accept=".zip"
            class="hidden"
            @change="handleFileUpload"
          />
          <FileText class="w-8 h-8 text-slate-600 mx-auto mb-3" />
          <p class="text-sm text-slate-400 font-medium">Click or drop project ZIP archives here for PHP code indexing</p>
          <p class="text-[10px] text-slate-500 mt-2">PHP source is parsed on demand — index a project to enable code search.</p>
        </div>

        <div v-if="uploading" class="mt-4 flex items-center gap-3 p-3 bg-emerald-950/10 border border-emerald-500/20 rounded-xl text-xs text-emerald-300">
          <div class="w-4 h-4 border-2 border-emerald-400 border-t-transparent rounded-full animate-spin"></div>
          <p class="text-[10px] text-indigo-400 mt-0.5">Storing project archive…</p>
        </div>

        <!-- Per-file resumable upload progress (large files, TUS path) -->
        <div v-if="Object.keys(uploadProgress).length > 0" class="mt-4 space-y-2">
          <div v-for="(progress, name) in uploadProgress" :key="name" class="p-3 bg-indigo-950/10 border border-indigo-500/20 rounded-xl">
            <div class="flex items-center justify-between text-[10px] text-indigo-300 mb-1.5">
              <span class="truncate font-mono">{{ name }}</span>
              <span class="font-mono">{{ (progress * 100).toFixed(0) }}%</span>
            </div>
            <div class="h-1.5 bg-slate-800 rounded-full overflow-hidden">
              <div class="h-full bg-indigo-500 rounded-full transition-all" :style="{ width: `${progress * 100}%` }"></div>
            </div>
          </div>
        </div>
      </div>

      <!-- Uploaded files list card -->
      <div class="bg-slate-900/30 border border-slate-800 rounded-2xl overflow-hidden">
        <div class="px-6 py-4 border-b border-slate-800 bg-[#0c101f]/30 flex items-center justify-between">
          <h4 class="text-xs font-bold tracking-wider uppercase text-slate-400">Indexed Files ({{ files.length }})</h4>
          <span class="text-[10px] text-slate-500 font-mono">Storage: Node service</span>
        </div>

        <!-- Empty state -->
        <div v-if="files.length === 0" class="p-10 text-center">
          <FileCode class="w-10 h-10 text-slate-700 mx-auto mb-3" />
          <p class="text-xs text-slate-500">No documents indexed in your vector store yet. Upload files to run RAG query generation.</p>
        </div>

        <!-- Table/List of files -->
        <div v-else class="divide-y divide-slate-800">
          <div
            v-for="file in files"
            :key="file.id"
            class="cursor-pointer"
          >
            <div
              class="px-6 py-4 flex items-center justify-between transition-all text-left"
              :class="activeFileId === file.id ? 'bg-indigo-500/10 border-l-2 border-indigo-500' : 'hover:bg-slate-900/20 border-l-2 border-transparent'"
              @click="toggleFileOptions(file, $event)"
            >
              <div class="flex items-center gap-3 min-w-0">
                <FileText class="w-5 h-5 text-indigo-400 shrink-0" />
                <div class="min-w-0">
                  <p class="text-sm font-medium text-slate-200 truncate">{{ file.name }}</p>
                  <p class="text-[10px] text-slate-500 font-mono">
                    {{ file.chunkCount }} chunk<span v-if="file.chunkCount !== 1">s</span> • {{ file.type.toUpperCase() }} • {{ (file.size / 1024).toFixed(1) }} KB
                  </p>
                  <p class="text-[10px] text-slate-500 font-mono">
                    <template v-if="file.projectId && file.commitSha">{{ file.projectId }} · {{ file.commitSha }}</template>
                    <template v-else>Legacy upload · no project/commit attribution</template>
                  </p>
                  <p v-if="file.projectId && file.commitSha" class="text-[10px] mt-0.5" :class="runtimeFor(file)?.deployment_mismatch ? 'text-amber-300' : runtimeFor(file)?.running_project_containers ? 'text-emerald-300' : 'text-slate-500'">
                    {{ runtimeFor(file)?.deployment_mismatch ? 'Warning: running deployment uses a different commit' : runtimeFor(file)?.running_project_containers ? `Running in Docker · ${runtimeFor(file)?.running_project_containers} container(s)` : runtimeLoadError ? 'Runtime deployment status unavailable' : 'No running Docker deployment reported' }}
                  </p>
                  <p v-if="file.status === 'failed'" class="text-[10px] text-red-400 mt-0.5 truncate" :title="file.error">
                    Failed: {{ file.error || 'Indexing failed.' }}
                  </p>
                </div>
                <span
                  v-if="file.status === 'processing'"
                  class="shrink-0 text-[10px] font-mono px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-400 border border-amber-500/20 flex items-center gap-1.5"
                >
                  <span class="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse"></span>
                  Processing…
                </span>
                <span
                  v-else-if="file.status === 'failed'"
                  class="shrink-0 text-[10px] font-mono px-2 py-0.5 rounded-full bg-red-500/10 text-red-400 border border-red-500/20"
                >
                  Failed
                </span>
                <span
                  v-if="parsingFileId === file.id"
                  class="shrink-0 text-[10px] font-mono px-2 py-0.5 rounded-full bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 flex items-center gap-1.5"
                >
                  <span class="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse"></span>
                  Indexing…
                </span>
              </div>
              <button
                @click.stop="confirmDelete(file)"
                class="p-2 rounded-lg text-slate-500 hover:text-red-400 hover:bg-red-500/10 transition-all shrink-0"
                title="Delete file"
              >
                <Trash2 class="w-4 h-4" />
              </button>
            </div>

            <!-- Options panel for selected file -->
            <div
              v-if="activeFileId === file.id"
              class="px-6 pb-4 pt-1 border-l-2 border-indigo-500/30 bg-indigo-500/5"
            >
              <p class="text-[10px] text-slate-500 mb-2 font-mono">
                {{ file.type === 'zip' ? 'PHP project archive — index to enable code search' : file.chunkCount > 0 ? 'Indexed — ' + file.chunkCount + ' chunks' : 'Not yet indexed — click Index to process' }}
              </p>
              <span
                v-if="indexedFileIds.has(file.id) && file.chunkCount > 0 && parsingFileId !== file.id"
                class="inline-flex items-center gap-1.5 text-[10px] font-mono px-2 py-1 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
              >
                <CheckCircle2 class="w-3 h-3" />
                Indexed in Qdrant
              </span>
              <button
                v-else-if="parsingFileId !== file.id"
                @click="handleIndexFile"
                class="flex items-center gap-1.5 px-3 py-1.5 text-[11px] font-semibold text-white bg-indigo-600 hover:bg-indigo-500 rounded-lg transition-colors shadow-sm shadow-indigo-600/20"
              >
                <Cpu class="w-3.5 h-3.5" />
                {{ file.chunkCount > 0 ? 'Re-index' : 'Index' }}
              </button>
              <button
                v-if="embeddingProvider === 'cohere' && file.projectId && file.commitSha && file.uploadSessionId && parsingFileId !== file.id"
                type="button"
                @click="emit('rebuildCohereEmbeddings', file.id, file.name)"
                class="flex items-center gap-1.5 px-3 py-1.5 text-[11px] font-semibold text-cyan-100 bg-cyan-700 hover:bg-cyan-600 rounded-lg transition-colors"
              >
                <Cpu class="w-3.5 h-3.5" />
                Rebuild Cohere embeddings
              </button>
              <div
                v-if="parsingFileId === file.id && indexingProgress.total > 0"
                class="space-y-2"
              >
                <div class="flex items-center justify-between text-[10px]">
                  <span class="text-indigo-400 font-mono" aria-live="polite">
                    {{ (indexingProgress.stage || indexingProgress.status) === 'upserting' ? 'Saving embeddings...' : (indexingProgress.stage === 'activating' ? 'Activating Cohere embeddings...' : (indexingProgress.stage === 'domain_embedding' ? `Embedding domain concepts: ${indexingProgress.indexed}/${indexingProgress.total}` : `Embedding chunks: ${indexingProgress.indexed}/${indexingProgress.total}`)) }}
                  </span>
                  <span class="text-indigo-400 tabular-nums" aria-hidden="true">
                    {{ ((indexingProgress.indexed / indexingProgress.total) * 100).toFixed(0) }}%
                  </span>
                </div>
                <div
                  class="h-1.5 bg-slate-800 rounded-full overflow-hidden"
                  role="progressbar"
                  :aria-valuenow="Math.min(100, Math.round((indexingProgress.indexed / indexingProgress.total) * 100))"
                  aria-valuemin="0"
                  aria-valuemax="100"
                  :aria-label="(indexingProgress.stage || indexingProgress.status) === 'upserting' ? 'Saving embeddings to the vector database' : (indexingProgress.stage === 'activating' ? 'Activating Cohere embeddings' : (indexingProgress.stage === 'domain_embedding' ? 'Embedding domain concepts' : 'Embedding project chunks'))"
                >
                  <div
                    class="h-full bg-indigo-500 rounded-full transition-all duration-300"
                    :style="{ width: `${(indexingProgress.indexed / indexingProgress.total) * 100}%` }"
                  ></div>
                </div>
              </div>
              <div
                v-else-if="parsingFileId === file.id"
                class="flex items-center gap-2 text-[11px] text-indigo-400"
              >
                <div class="w-3.5 h-3.5 border-2 border-indigo-400 border-t-transparent rounded-full animate-spin"></div>
                {{ progressStageMessage[indexingProgress.stage || indexingProgress.status] || 'Extracting PHP code...' }}
              </div>
              <p v-if="indexingProgress.status === 'error' && activeFileId === file.id" role="alert" class="text-[10px] text-rose-300">
                {{ indexingProgress.error || 'Embedding failed. Check the API service and retry.' }}
              </p>
              <p v-if="parsingFileId === file.id && indexingProgress.graph_status" class="text-[10px] text-slate-400" aria-live="polite">
                Neo4j graph: {{ indexingProgress.graph_status === 'ready' && indexingProgress.graph_index_complete ? `complete (${indexingProgress.graph_nodes ?? 0} symbols, ${indexingProgress.graph_relations ?? 0} relationships)` : indexingProgress.graph_status }}
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>

    <ConfirmModal
      :show="showDeleteConfirm"
      title="Delete File"
      :message="`Are you sure you want to delete ${selectedFile?.name ?? ''}? This action cannot be undone.`"
      @confirm="handleDeleteConfirm"
      @cancel="handleDeleteCancel"
    />
  </div>
</template>
