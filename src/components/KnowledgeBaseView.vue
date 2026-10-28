<script setup lang="ts">
import { ref } from 'vue';
import { BookOpen, FileText, GitBranch, Scale } from 'lucide-vue-next';
import type { UploadedFile, RuntimeProjectRecord, RAGConfig } from '../types';
import { useUrlTab } from '../composables/useUrlTab';
import RAGFiles from './RAGFiles.vue';
import GitSourceSetup from './code-quality/GitSourceSetup.vue';
import BusinessRuleManager from './code-quality/BusinessRuleManager.vue';

const props = defineProps<{
  files: UploadedFile[];
  uploading: boolean;
  uploadProgress: Record<string, number>;
  parsingFileId: string | null;
  indexingProgress: { total: number; indexed: number; status: string; stage?: string; error?: string };
  indexedFileIds: Set<string>;
  embeddingProvider: RAGConfig['embeddingProvider'];
  projectId: string;
}>();
const emit = defineEmits<{
  uploadFiles: [files: FileList];
  deleteFile: [id: string, name: string];
  indexFile: [id: string, name: string];
  rebuildCohereEmbeddings: [id: string, name: string];
}>();

type KnowledgeTab = 'files' | 'git-source' | 'business-rules';
const activeTab = useUrlTab<KnowledgeTab>({ values: ['files', 'git-source', 'business-rules'], fallback: 'files' });
</script>

<template>
  <section class="flex min-h-0 flex-1 flex-col p-4 text-slate-100 sm:p-6" aria-label="Knowledge Base">
    <div class="mx-auto flex min-h-0 w-full max-w-7xl flex-1 flex-col gap-5">
      <header class="border-b border-slate-800 pb-4">
        <div class="flex items-center gap-2 text-indigo-300"><BookOpen :size="17" /><span class="text-xs font-semibold uppercase tracking-[0.18em]">Knowledge Base</span></div>
        <h1 class="mt-1 text-2xl font-semibold">Project knowledge</h1>
        <p class="mt-1 text-sm text-slate-400">Manage indexed files, Git source revisions, and Outline business rules.</p>
      </header>
      <div role="tablist" aria-label="Knowledge Base sections" class="flex flex-wrap gap-2 border-b border-slate-800">
        <button type="button" role="tab" :aria-selected="activeTab === 'files'" class="inline-flex min-h-10 items-center gap-2 rounded-t-lg border-b-2 px-3 text-sm" :class="activeTab === 'files' ? 'border-indigo-300 text-indigo-100' : 'border-transparent text-slate-400 hover:text-slate-200'" @click="activeTab = 'files'"><FileText :size="15" />Files & indexing</button>
        <button type="button" role="tab" :aria-selected="activeTab === 'git-source'" class="inline-flex min-h-10 items-center gap-2 rounded-t-lg border-b-2 px-3 text-sm" :class="activeTab === 'git-source' ? 'border-indigo-300 text-indigo-100' : 'border-transparent text-slate-400 hover:text-slate-200'" @click="activeTab = 'git-source'"><GitBranch :size="15" />Git source</button>
        <button type="button" role="tab" :aria-selected="activeTab === 'business-rules'" class="inline-flex min-h-10 items-center gap-2 rounded-t-lg border-b-2 px-3 text-sm" :class="activeTab === 'business-rules' ? 'border-indigo-300 text-indigo-100' : 'border-transparent text-slate-400 hover:text-slate-200'" @click="activeTab = 'business-rules'"><Scale :size="15" />Business rules</button>
      </div>
      <RAGFiles v-if="activeTab === 'files'" :files="props.files" :uploading="props.uploading" :upload-progress="props.uploadProgress" :parsing-file-id="props.parsingFileId" :indexing-progress="props.indexingProgress" :indexed-file-ids="props.indexedFileIds" :embedding-provider="props.embeddingProvider" @upload-files="emit('uploadFiles', $event)" @delete-file="(id, name) => emit('deleteFile', id, name)" @index-file="(id, name) => emit('indexFile', id, name)" @rebuild-cohere-embeddings="(id, name) => emit('rebuildCohereEmbeddings', id, name)" />
      <div v-else-if="activeTab === 'git-source'" role="tabpanel" class="min-h-0 flex-1 overflow-y-auto pt-1">
        <GitSourceSetup :default-project-id="props.projectId" />
      </div>
      <div v-else role="tabpanel" class="min-h-0 flex-1 overflow-y-auto pt-1">
        <BusinessRuleManager :project-id="props.projectId" />
      </div>
    </div>
  </section>
</template>
