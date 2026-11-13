<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { Files, GitBranch, Scale, UploadCloud, FileArchive, CircleCheck, CircleAlert } from 'lucide-vue-next';
import type { UploadedFile, RAGConfig } from '../types';
import { useUrlTab } from '../composables/useUrlTab';
import { makeRequest } from '../ui/makeRequest';
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

type KnowledgeTab = 'files' | 'git-source' | 'business-rules';
const activeTab = useUrlTab<KnowledgeTab>({ values: ['files', 'git-source', 'business-rules'], fallback: 'files' });

// ---- Upload identity (owned by App; this card edits it) --------------------
const projectInput = ref<HTMLInputElement | null>(null);
const commitInput = ref<HTMLInputElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const uploadValidation = computed(() => validateUploadIdentity(props.projectId, props.commitSha));
const canUpload = computed(() => !!uploadValidation.value.identity && !props.uploading);
const uploadBusy = computed(() => props.uploading || Object.keys(props.uploadProgress).length > 0);
const knownProjectIds = computed(() => [...new Set(props.files.map((file) => file.projectId).filter(Boolean))]);
const focusUploadIdentity = () => {
  if (uploadValidation.value.projectError) projectInput.value?.focus();
  else commitInput.value?.focus();
};

const dragOver = ref(false);
const chooseFiles = () => {
  if (!canUpload.value) {
    focusUploadIdentity();
    return;
  }
  fileInput.value?.click();
};
const onFilesChosen = (event: Event) => {
  const target = event.target as HTMLInputElement;
  if (!canUpload.value) {
    target.value = '';
    focusUploadIdentity();
    return;
  }
  if (target.files?.length) emit('uploadFiles', target.files);
  target.value = '';
};
const onDrop = (event: DragEvent) => {
  dragOver.value = false;
  if (!canUpload.value) {
    focusUploadIdentity();
    return;
  }
  if (event.dataTransfer?.files?.length) emit('uploadFiles', event.dataTransfer.files);
};

// ---- Summary numbers --------------------------------------------------------
const stats = computed(() => {
  const projects = new Set(props.files.map((file) => file.projectId || '__legacy'));
  const chunks = props.files.reduce((sum, file) => sum + (file.chunkCount || 0), 0);
  const indexed = props.files.filter((file) => file.chunkCount > 0 && file.status !== 'failed').length;
  return { projects: projects.size, revisions: props.files.length, chunks, indexed };
});

// ---- Tab status dots: is a Git source synced / an Outline collection connected? ----
const gitReady = ref(false);
const rulesConnected = ref(false);
let statusTimer: ReturnType<typeof setTimeout> | null = null;
let statusSequence = 0;
const loadStatus = async (projectId: string) => {
  const sequence = ++statusSequence;
  gitReady.value = false;
  rulesConnected.value = false;
  if (!projectId) return;
  const base = `/api/code-quality/projects/${encodeURIComponent(projectId)}`;
  const read = async (path: string) => {
    try {
      const response = await makeRequest(path);
      return response.ok ? await response.json() : null;
    } catch { return null; }
  };
  const [git, outline] = await Promise.all([read(`${base}/git-source`), read(`${base}/outline`)]);
  if (sequence !== statusSequence) return;
  gitReady.value = git?.source?.status === 'ready';
  rulesConnected.value = !!outline?.connection;
};
watch(() => [props.projectId.trim(), activeTab.value] as const, ([projectId]) => {
  if (statusTimer) clearTimeout(statusTimer);
  statusTimer = setTimeout(() => void loadStatus(projectId), 500);
}, { immediate: true });
onUnmounted(() => { if (statusTimer) clearTimeout(statusTimer); });

const tabs = computed(() => [
  { id: 'files' as const, label: 'Files and indexing', icon: Files, ready: false, readyLabel: '' },
  { id: 'git-source' as const, label: 'Git source', icon: GitBranch, ready: gitReady.value, readyLabel: 'Synced' },
  { id: 'business-rules' as const, label: 'Business rules', icon: Scale, ready: rulesConnected.value, readyLabel: 'Connected' },
]);
</script>

<template>
  <section class="kb-page" aria-label="Knowledge base">
    <div class="kb-wrap">
      <div class="kb-topbar">
        <div class="kb-tabs" role="tablist" aria-label="Knowledge base sections">
          <button
            v-for="tab in tabs"
            :key="tab.id"
            type="button"
            role="tab"
            :aria-selected="activeTab === tab.id"
            @click="activeTab = tab.id"
          >
            <component :is="tab.icon" aria-hidden="true" />{{ tab.label }}
            <span v-if="tab.ready" class="kb-tab-dot" :title="tab.readyLabel"><span class="sr-only">({{ tab.readyLabel }})</span></span>
          </button>
        </div>
        <div v-if="activeTab === 'files' && files.length" class="kb-stats" aria-label="Summary">
          <span class="kb-stat"><b>{{ stats.projects }}</b>{{ stats.projects === 1 ? 'project' : 'projects' }}</span>
          <span class="kb-stat"><b>{{ stats.revisions }}</b>{{ stats.revisions === 1 ? 'revision' : 'revisions' }}</span>
          <span class="kb-stat"><b>{{ stats.chunks.toLocaleString() }}</b>chunks</span>
          <span class="kb-stat"><b>{{ stats.indexed }}/{{ stats.revisions }}</b>indexed</span>
        </div>
      </div>

      <div v-if="activeTab === 'files'" role="tabpanel" aria-label="Files and indexing" class="kb-grid">
        <RAGFiles
          :files="props.files"
          :parsing-file-id="props.parsingFileId"
          :indexing-progress="props.indexingProgress"
          :indexed-file-ids="props.indexedFileIds"
          :embedding-provider="props.embeddingProvider"
          @choose-upload="chooseFiles"
          @connect-git="activeTab = 'git-source'"
          @delete-file="(id, name) => emit('deleteFile', id, name)"
          @index-file="(id, name) => emit('indexFile', id, name)"
          @rebuild-cohere-embeddings="(id, name) => emit('rebuildCohereEmbeddings', id, name)"
        />

        <aside class="kb-aside">
          <fieldset class="kb-upload" :disabled="uploadBusy">
            <legend class="kb-upload-title"><UploadCloud aria-hidden="true" />Upload a new revision</legend>
            <p id="knowledge-upload-help" class="kb-lead">Pick a project ID and drop a ZIP. The server automatically creates the revision ID and verifies the archive checksum. No Git repository or commit SHA is required.</p>

            <div class="kb-field">
              <label for="knowledge-upload-project">Project ID</label>
              <input
                id="knowledge-upload-project"
                ref="projectInput"
                class="kb-input"
                :value="props.projectId"
                list="knowledge-project-ids"
                maxlength="128"
                required
                autocomplete="off"
                aria-describedby="knowledge-project-help"
                :aria-invalid="!!props.projectId.trim() && !!uploadValidation.projectError"
                placeholder="pizza"
                @input="emit('update:projectId', ($event.target as HTMLInputElement).value)"
              />
              <datalist id="knowledge-project-ids"><option v-for="project in knownProjectIds" :key="project" :value="project" /></datalist>
              <span id="knowledge-project-help" class="kb-help" :data-tone="props.projectId.trim() && uploadValidation.projectError ? 'bad' : undefined">{{ (props.projectId.trim() && uploadValidation.projectError) || 'Use the same ID for later revisions of this project.' }}</span>
            </div>

            <div class="kb-field">
              <label for="knowledge-upload-commit">Commit SHA <em>(optional)</em></label>
              <input
                id="knowledge-upload-commit"
                ref="commitInput"
                class="kb-input"
                :value="props.commitSha"
                maxlength="64"
                spellcheck="false"
                autocomplete="off"
                aria-describedby="knowledge-commit-help"
                :aria-invalid="!!props.commitSha.trim() && !!uploadValidation.commitError"
                placeholder="Leave empty for an automatic upload revision"
                @input="emit('update:commitSha', ($event.target as HTMLInputElement).value)"
              />
              <span id="knowledge-commit-help" class="kb-help" :data-tone="uploadValidation.commitError ? 'bad' : undefined">{{ uploadValidation.commitError || 'Only add a real Git commit if you want to link this upload to it.' }}</span>
            </div>

            <input ref="fileInput" id="file-upload-input" type="file" multiple accept=".zip" class="hidden" :disabled="!canUpload" @change="onFilesChosen" />
            <button
              type="button"
              class="kb-drop"
              :class="{ 'is-over': dragOver }"
              :aria-disabled="!canUpload"
              aria-describedby="knowledge-upload-help"
              @click="chooseFiles"
              @dragover.prevent="dragOver = true"
              @dragleave="dragOver = false"
              @drop.prevent="onDrop"
            >
              <span class="kb-drop-icon"><FileArchive aria-hidden="true" /></span>
              <b>{{ canUpload ? 'Choose or drop ZIP archives' : uploadBusy ? 'Upload in progress…' : 'Add a project ID first' }}</b>
              <span>{{ canUpload ? 'Source is parsed when you index it.' : 'Then choose or drop ZIP archives.' }}</span>
            </button>

            <p role="status" aria-live="polite" class="kb-ready" :data-tone="uploadValidation.identity ? 'ok' : 'warn'">
              <CircleCheck v-if="uploadValidation.identity" aria-hidden="true" />
              <CircleAlert v-else aria-hidden="true" />
              {{ uploadValidation.identity ? 'Ready to upload. Your revision ID will appear in the list.' : uploadValidation.projectError || uploadValidation.commitError }}
            </p>

            <div v-if="uploading || Object.keys(uploadProgress).length" class="kb-uploads">
              <div v-if="uploading && !Object.keys(uploadProgress).length" class="kb-upload-row">
                <div class="kb-progress-top"><span>Storing project archive…</span></div>
                <div class="kb-track" data-indeterminate="true"></div>
              </div>
              <div v-for="(progress, name) in uploadProgress" :key="name" class="kb-upload-row">
                <div class="kb-progress-top"><span class="truncate">{{ name }}</span><span>{{ (progress * 100).toFixed(0) }}%</span></div>
                <div class="kb-track" role="progressbar" :aria-label="`Uploading ${name}`" :aria-valuenow="Math.round(progress * 100)" aria-valuemin="0" aria-valuemax="100">
                  <i :style="{ transform: `scaleX(${progress})` }"></i>
                </div>
              </div>
            </div>
          </fieldset>
        </aside>
      </div>

      <div v-else-if="activeTab === 'git-source'" role="tabpanel" aria-label="Git source">
        <GitSourceSetup :default-project-id="props.projectId" />
      </div>
      <div v-else role="tabpanel" aria-label="Business rules">
        <BusinessRuleManager :project-id="props.projectId" />
      </div>
    </div>
  </section>
</template>
