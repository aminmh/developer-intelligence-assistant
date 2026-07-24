<script setup lang="ts">
import { ref } from 'vue';
import { 
  FileText, 
  Upload, 
  Trash2, 
  FileCode, 
  Search 
} from 'lucide-vue-next';
import { UploadedFile, SearchMatch } from '../types';
import ConfirmModal from './ConfirmModal.vue';

const props = defineProps<{
  files: UploadedFile[];
  uploading: boolean;
  uploadProgress: Record<string, number>;
  searchMatches: SearchMatch[];
  searchLoading: boolean;
}>();

const emit = defineEmits<{
  (e: 'uploadFiles', files: FileList): void;
  (e: 'deleteFile', id: string, name: string): void;
  (e: 'performSearchOnly', query: string): void;
}>();

const dragOver = ref(false);
const searchInput = ref('');
const showDeleteConfirm = ref(false);
const selectedFile = ref<UploadedFile | null>(null);

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

const onSearch = () => {
  if (searchInput.value.trim()) {
    emit('performSearchOnly', searchInput.value.trim());
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
            Upload Source Documents
          </h4>
          <span class="text-[10px] text-slate-500">Accepted: PDF, ZIP, MD, TXT, JS, TS, JSON</span>
        </div>

        <div
          @click="triggerUpload"
          class="border-2 border-dashed border-slate-800 rounded-xl p-8 text-center cursor-pointer hover:border-indigo-500/50 transition-all"
        >
          <input
            id="file-upload-input"
            type="file"
            multiple
            accept=".pdf,.zip,.md,.txt,.js,.ts,.tsx,.json"
            class="hidden"
            @change="handleFileUpload"
          />
          <FileText class="w-8 h-8 text-slate-600 mx-auto mb-3" />
          <p class="text-sm text-slate-400 font-medium">Click or drop files here for indexing & embeddings generation</p>
          <p class="text-[10px] text-slate-500 mt-2">Maximum file size: depends on your runtime environment limits.</p>
        </div>

        <div v-if="uploading" class="mt-4 flex items-center gap-3 p-3 bg-emerald-950/10 border border-emerald-500/20 rounded-xl text-xs text-emerald-300">
          <div class="w-4 h-4 border-2 border-emerald-400 border-t-transparent rounded-full animate-spin"></div>
          <p class="text-[10px] text-indigo-400 mt-0.5">We are generating context chunks based on your configured chunk size, computing high-dimensional vectors, and populating your vectorDB.</p>
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
          <span class="text-[10px] text-slate-500 font-mono">Storage: Node Sandbox</span>
        </div>

        <!-- Empty state -->
        <div v-if="files.length === 0" class="p-10 text-center">
          <FileCode class="w-10 h-10 text-slate-700 mx-auto mb-3" />
          <p class="text-xs text-slate-500">No documents indexed in your vector store yet. Upload files to run RAG query generation.</p>
        </div>

        <!-- Table/List of files -->
        <div v-else class="divide-y divide-slate-800">
          <div v-for="file in files" :key="file.id" class="px-6 py-4 flex items-center justify-between hover:bg-slate-900/20 transition-all text-left">
            <div class="flex items-center gap-3 min-w-0">
              <FileText class="w-5 h-5 text-indigo-400 shrink-0" />
              <div class="min-w-0">
                <p class="text-sm font-medium text-slate-200 truncate">{{ file.name }}</p>
                <p class="text-[10px] text-slate-500 font-mono">
                  {{ file.chunkCount }} chunk<span v-if="file.chunkCount !== 1">s</span> • {{ file.type.toUpperCase() }} • {{ (file.size / 1024).toFixed(1) }} KB
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
            </div>
            <button
              @click="confirmDelete(file)"
              class="p-2 rounded-lg text-slate-500 hover:text-red-400 hover:bg-red-500/10 transition-all shrink-0"
              title="Delete file"
            >
              <Trash2 class="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>

      <!-- Semantic Search Only Card -->
      <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
        <h4 class="text-xs font-bold tracking-wider uppercase text-slate-400 flex items-center gap-2">
          <Search class="w-4 h-4 text-indigo-400" />
          Semantic Search (No LLM)
        </h4>
        <p class="text-[10px] text-slate-500 -mt-2">Run a retrieval-only search against your indexed file embeddings and preview matching context chunks.</p>

        <div class="flex gap-3">
          <input 
            v-model="searchInput"
            type="text"
            placeholder="Search query string..."
            class="flex-1 bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-indigo-500 text-slate-200"
            @keyup.enter="onSearch"
          />
          <button 
            @click="onSearch"
            :disabled="searchLoading || !searchInput.trim()"
            class="px-5 py-2.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl text-xs font-semibold transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1.5"
          >
            <Search class="w-3.5 h-3.5" />
            {{ searchLoading ? 'Searching...' : 'Search' }}
          </button>
        </div>

        <!-- Search Results -->
        <div v-if="searchMatches.length > 0" class="space-y-3 pt-2">
          <div 
            v-for="(match, i) in searchMatches" 
            :key="i"
            class="bg-[#11162d] border border-slate-800 rounded-xl p-4"
          >
            <div class="flex items-center justify-between text-[10px] font-mono text-slate-400 mb-2">
              <span class="flex items-center gap-1">
                <FileText class="w-3 h-3" />
                {{ match.chunk.fileName }} • Chunk #{{ match.chunk.index }}
              </span>
              <span class="bg-indigo-600/20 text-indigo-400 px-2 py-0.5 rounded-full">
                Score: {{ (match.score * 100).toFixed(0) }}%
              </span>
            </div>
            <p class="text-[11px] text-slate-300 leading-relaxed font-mono">
              {{ match.chunk.text }}
            </p>
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
