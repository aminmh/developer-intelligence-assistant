<script setup lang="ts">
import { computed, ref } from 'vue';
import { BookOpen, FileText, GitBranch, Scale } from 'lucide-vue-next';
import type { UploadedFile, RuntimeProjectRecord, RAGConfig } from '../types';
import { useUrlTab } from '../composables/useUrlTab';
import RAGFiles from './RAGFiles.vue';
import GitSourceSetup from './code-quality/GitSourceSetup.vue';
import BusinessRuleManager from './code-quality/BusinessRuleManager.vue';
import { validateUploadIdentity } from '../services/uploadIdentity';

const props = defineProps<{
  files: UploadedFile[];
  uploading: boolean;
  uploadProgress: Record<string, number>;
  parsingFileId: string | null;
  indexingProgress: { total: number; indexed: number; status: string; stage?: string; error?: string };
  indexedFileIds: Set<string>;
  embeddingProvider: RAGConfig['embeddingProvider'];
  projectId: string;
  commitSha: string;
}>();
const emit = defineEmits<{
  uploadFiles: [files: FileList];
  deleteFile: [id: string, name: string];
  indexFile: [id: string, name: string];
  rebuildCohereEmbeddings: [id: string, name: string];
  'update:projectId': [value: string];
  'update:commitSha': [value: string];
}>();

const projectInput = ref<HTMLInputElement | null>(null);
const commitInput = ref<HTMLInputElement | null>(null);
const uploadValidation = computed(() => validateUploadIdentity(props.projectId, props.commitSha));
const knownProjectIds = computed(() => [...new Set(props.files.map((file) => file.projectId).filter(Boolean))]);
const focusUploadIdentity = () => {
  if (uploadValidation.value.projectError) projectInput.value?.focus();
  else commitInput.value?.focus();
};

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
      <fieldset class="rounded-xl border border-slate-700 bg-slate-900/50 p-4" :disabled="props.uploading || Object.keys(props.uploadProgress).length > 0">
        <legend class="px-1 text-sm font-semibold text-slate-200">Project revision</legend>
        <p id="knowledge-upload-help" class="text-sm text-slate-400">Choose a project ID and upload your ZIP. The server automatically creates a revision ID and verifies the archive checksum. No Git repository or commit SHA is required.</p>
        <div class="mt-3 grid gap-3 sm:grid-cols-[minmax(10rem,1fr)_minmax(0,3fr)]">
          <div>
            <label for="knowledge-upload-project" class="mb-1 block text-xs font-medium text-slate-300">Project ID</label>
            <input id="knowledge-upload-project" ref="projectInput" :value="props.projectId" list="knowledge-project-ids" maxlength="128" required aria-describedby="knowledge-project-help" :aria-invalid="!!props.projectId.trim() && !!uploadValidation.projectError" class="min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm outline-none focus:border-indigo-400" placeholder="e.g. pizza" @input="emit('update:projectId', ($event.target as HTMLInputElement).value)" />
            <datalist id="knowledge-project-ids"><option v-for="project in knownProjectIds" :key="project" :value="project" /></datalist>
            <p id="knowledge-project-help" class="mt-1 text-xs text-slate-400">{{ uploadValidation.projectError || 'Use the same ID for later revisions of this project.' }}</p>
          </div>
          <div>
            <label for="knowledge-upload-commit" class="mb-1 block text-xs font-medium text-slate-300">Commit SHA (optional)</label>
            <input id="knowledge-upload-commit" ref="commitInput" :value="props.commitSha" maxlength="64" spellcheck="false" autocomplete="off" aria-describedby="knowledge-commit-help" :aria-invalid="!!props.commitSha.trim() && !!uploadValidation.commitError" class="min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 font-mono text-xs outline-none focus:border-indigo-400" placeholder="Leave empty for an automatic upload revision" @input="emit('update:commitSha', ($event.target as HTMLInputElement).value)" />
            <p id="knowledge-commit-help" class="mt-1 text-xs text-slate-400">{{ uploadValidation.commitError || 'Only supply a real Git commit if you want to link this upload to it.' }}</p>
          </div>
        </div>
        <p role="status" aria-live="polite" class="mt-3 text-xs" :class="uploadValidation.identity ? 'text-emerald-300' : 'text-amber-200'">{{ uploadValidation.identity ? 'Ready to upload. Your revision ID will appear in the file list.' : uploadValidation.projectError || uploadValidation.commitError }}</p>
      </fieldset>
      <div role="tablist" aria-label="Knowledge Base sections" class="flex flex-wrap gap-2 border-b border-slate-800">
        <button type="button" role="tab" :aria-selected="activeTab === 'files'" class="inline-flex min-h-10 items-center gap-2 rounded-t-lg border-b-2 px-3 text-sm" :class="activeTab === 'files' ? 'border-indigo-300 text-indigo-100' : 'border-transparent text-slate-400 hover:text-slate-200'" @click="activeTab = 'files'"><FileText :size="15" />Files & indexing</button>
        <button type="button" role="tab" :aria-selected="activeTab === 'git-source'" class="inline-flex min-h-10 items-center gap-2 rounded-t-lg border-b-2 px-3 text-sm" :class="activeTab === 'git-source' ? 'border-indigo-300 text-indigo-100' : 'border-transparent text-slate-400 hover:text-slate-200'" @click="activeTab = 'git-source'"><GitBranch :size="15" />Git source</button>
        <button type="button" role="tab" :aria-selected="activeTab === 'business-rules'" class="inline-flex min-h-10 items-center gap-2 rounded-t-lg border-b-2 px-3 text-sm" :class="activeTab === 'business-rules' ? 'border-indigo-300 text-indigo-100' : 'border-transparent text-slate-400 hover:text-slate-200'" @click="activeTab = 'business-rules'"><Scale :size="15" />Business rules</button>
      </div>
      <RAGFiles v-if="activeTab === 'files'" :files="props.files" :uploading="props.uploading" :upload-progress="props.uploadProgress" :parsing-file-id="props.parsingFileId" :indexing-progress="props.indexingProgress" :indexed-file-ids="props.indexedFileIds" :embedding-provider="props.embeddingProvider" :can-upload="!!uploadValidation.identity" @request-upload-identity="focusUploadIdentity" @upload-files="emit('uploadFiles', $event)" @delete-file="(id, name) => emit('deleteFile', id, name)" @index-file="(id, name) => emit('indexFile', id, name)" @rebuild-cohere-embeddings="(id, name) => emit('rebuildCohereEmbeddings', id, name)" />
      <div v-else-if="activeTab === 'git-source'" role="tabpanel" class="min-h-0 flex-1 overflow-y-auto pt-1">
        <GitSourceSetup :default-project-id="props.projectId" />
      </div>
      <div v-else role="tabpanel" class="min-h-0 flex-1 overflow-y-auto pt-1">
        <BusinessRuleManager :project-id="props.projectId" />
      </div>
    </div>
  </section>
</template>
