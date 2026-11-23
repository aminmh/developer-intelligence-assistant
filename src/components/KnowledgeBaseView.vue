<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { Files, FileJson, GitBranch, Scale, UploadCloud, FileArchive, CircleCheck, CircleAlert, Filter, GitCommitHorizontal, TriangleAlert, Check } from 'lucide-vue-next';
import type { UploadedFile, RAGConfig } from '../types';
import { useUrlTab } from '../composables/useUrlTab';
import { makeRequest } from '../ui/makeRequest';
import RAGFiles from './RAGFiles.vue';
import GitSourceSetup from './code-quality/GitSourceSetup.vue';
import BusinessRuleManager from './code-quality/BusinessRuleManager.vue';
import ApiContractManager from './contracts/ApiContractManager.vue';
import { validateUploadIdentity } from '../services/uploadIdentity';
import { useProjectScope, type ScopeRevision } from '../composables/useProjectScope';
import { describeRevision, relativeAge } from '../utils/chatRevisions';

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

type KnowledgeTab = 'files' | 'git-source' | 'business-rules' | 'api-contracts';
const activeTab = useUrlTab<KnowledgeTab>({ values: ['files', 'git-source', 'business-rules', 'api-contracts'], fallback: 'files' });

// ---- Upload identity (owned by App; this card edits it) --------------------
const projectInput = ref<HTMLInputElement | null>(null);
const commitInput = ref<HTMLInputElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const uploadValidation = computed(() => validateUploadIdentity(props.projectId, props.commitSha));
const canUpload = computed(() => !!uploadValidation.value.identity && !props.uploading);
const uploadBusy = computed(() => props.uploading || Object.keys(props.uploadProgress).length > 0);
// ---- The selected project (header switcher) ------------------------------
const scope = useProjectScope();
const showAllProjects = ref(false);
const visibleFiles = computed(() => showAllProjects.value || !props.projectId.trim()
  ? props.files
  : props.files.filter((file) => file.projectId === props.projectId.trim()));
const projectRevisions = computed(() => scope.currentProject.value?.revisions ?? []);
const revisionSource = (rev: ScopeRevision) => rev.source_kind === 'git' ? 'Git sync' : rev.source_kind === 'working_tree' ? 'Working tree' : 'ZIP upload';
const focusUploadIdentity = () => {
  if (uploadValidation.value.projectError) document.querySelector<HTMLButtonElement>('.ps-trigger')?.click();
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
  const projects = new Set(visibleFiles.value.map((file) => file.projectId || '__legacy'));
  const chunks = visibleFiles.value.reduce((sum, file) => sum + (file.chunkCount || 0), 0);
  const indexed = visibleFiles.value.filter((file) => file.chunkCount > 0 && file.status !== 'failed').length;
  return { projects: projects.size, revisions: visibleFiles.value.length, chunks, indexed };
});

// ---- Tab status dots: is a Git source synced / an Outline collection connected? ----
const gitReady = ref(false);
const rulesConnected = ref(false);
const hasContracts = ref(false);
let statusTimer: ReturnType<typeof setTimeout> | null = null;
let statusSequence = 0;
const loadStatus = async (projectId: string) => {
  const sequence = ++statusSequence;
  gitReady.value = false;
  rulesConnected.value = false;
  hasContracts.value = false;
  if (!projectId) return;
  const base = `/api/code-quality/projects/${encodeURIComponent(projectId)}`;
  const read = async (path: string) => {
    try {
      const response = await makeRequest(path);
      return response.ok ? await response.json() : null;
    } catch { return null; }
  };
  const [git, outline, contracts] = await Promise.all([read(`${base}/git-source`), read(`${base}/outline`),
    read(`/api/projects/${encodeURIComponent(projectId)}/contracts`)]);
  if (sequence !== statusSequence) return;
  gitReady.value = git?.source?.status === 'ready';
  rulesConnected.value = !!outline?.connection;
  hasContracts.value = (contracts?.sources?.length || 0) + (contracts?.attachments?.length || 0) > 0;
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
  { id: 'api-contracts' as const, label: 'API contracts', icon: FileJson, ready: hasContracts.value, readyLabel: 'Has contracts' },
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
        <div v-if="activeTab === 'files' && visibleFiles.length" class="kb-stats" aria-label="Summary">
          <span class="kb-stat"><b>{{ stats.projects }}</b>{{ stats.projects === 1 ? 'project' : 'projects' }}</span>
          <span class="kb-stat"><b>{{ stats.revisions }}</b>{{ stats.revisions === 1 ? 'revision' : 'revisions' }}</span>
          <span class="kb-stat"><b>{{ stats.chunks.toLocaleString() }}</b>chunks</span>
          <span class="kb-stat"><b>{{ stats.indexed }}/{{ stats.revisions }}</b>indexed</span>
        </div>
      </div>

      <div v-if="activeTab === 'files'" role="tabpanel" aria-label="Files and indexing" class="kb-grid">
        <div class="kb-main">
        <div v-if="props.projectId.trim()" class="kb-scope">
          <Filter aria-hidden="true" />
          <span v-if="!showAllProjects">Showing <b>{{ props.projectId.trim() }}</b> only</span>
          <span v-else>Showing all projects</span>
          <button type="button" class="kb-link" @click="showAllProjects = !showAllProjects">{{ showAllProjects ? `Show ${props.projectId.trim()} only` : 'Show all projects' }}</button>
        </div>

        <section v-if="props.projectId.trim() && !showAllProjects" class="kb-panel kb-revisions" aria-labelledby="kb-revisions-title">
          <h2 id="kb-revisions-title" class="kb-panel-title kb-panel-title-sm"><GitBranch aria-hidden="true" />{{ props.projectId.trim() }} · revisions</h2>
          <p v-if="!projectRevisions.length" class="kb-lead">No revisions yet. Upload a ZIP or connect a Git source to create the first one.</p>
          <ul v-else class="kb-rev-list">
            <li v-for="rev in projectRevisions" :key="rev.revision_id" class="kb-rev-item">
              <GitCommitHorizontal aria-hidden="true" />
              <span class="kb-rev-text">
                <b>{{ describeRevision({ ...rev, created_at: null }) }}</b>
                <span>{{ revisionSource(rev) }}<template v-if="relativeAge(rev.created_at)"> · {{ relativeAge(rev.created_at) }}</template><template v-if="rev.vectors_missing"> · indexed data missing</template></span>
              </span>
              <span v-if="rev.indexed" class="kb-tag" data-tone="ok"><Check aria-hidden="true" />Indexed</span>
              <span v-else class="kb-tag" data-tone="warn"><TriangleAlert aria-hidden="true" />{{ rev.vectors_missing ? 'Data missing' : rev.state === 'failed' ? 'Failed' : 'Not indexed' }}</span>
            </li>
          </ul>
        </section>

        <RAGFiles
          v-if="visibleFiles.length || showAllProjects || !projectRevisions.length"
          :files="visibleFiles"
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
        </div>

        <aside class="kb-aside">
          <fieldset class="kb-upload" :disabled="uploadBusy">
            <legend class="kb-upload-title"><UploadCloud aria-hidden="true" />Upload a new revision</legend>
            <p id="knowledge-upload-help" class="kb-lead">Drop a ZIP for the selected project. The server automatically creates the revision ID and verifies the archive checksum. No Git repository or commit SHA is required.</p>

            <div class="kb-field">
              <label for="knowledge-upload-project">Project ID</label>
              <input
                id="knowledge-upload-project"
                ref="projectInput"
                class="kb-input"
                :value="props.projectId"
                readonly
                aria-describedby="knowledge-project-help"
                :aria-invalid="!props.projectId.trim() || !!uploadValidation.projectError"
                placeholder="Choose a project in the header"
              />
              <span id="knowledge-project-help" class="kb-help" :data-tone="props.projectId.trim() && uploadValidation.projectError ? 'bad' : undefined">{{ (props.projectId.trim() && uploadValidation.projectError) || 'Uploads go to the project chosen in the header. Type a new ID there to start a project.' }}</span>
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
              <b>{{ canUpload ? 'Choose or drop ZIP archives' : uploadBusy ? 'Upload in progress…' : props.projectId.trim() ? 'Fix the commit SHA first' : 'Choose a project in the header first' }}</b>
              <span>{{ canUpload ? 'Source is parsed when you index it.' : 'Then choose or drop ZIP archives.' }}</span>
            </button>

            <p role="status" aria-live="polite" class="kb-ready" :data-tone="uploadValidation.identity ? 'ok' : 'warn'">
              <CircleCheck v-if="uploadValidation.identity" aria-hidden="true" />
              <CircleAlert v-else aria-hidden="true" />
              {{ uploadValidation.identity ? `Ready to upload to ${props.projectId.trim()}.` : !props.projectId.trim() ? 'Choose or start a project in the header switcher.' : uploadValidation.projectError || uploadValidation.commitError }}
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
      <div v-else-if="activeTab === 'business-rules'" role="tabpanel" aria-label="Business rules">
        <BusinessRuleManager :project-id="props.projectId" />
      </div>
      <div v-else role="tabpanel" aria-label="API contracts">
        <ApiContractManager :project-id="props.projectId" @changed="hasContracts = $event" />
      </div>
    </div>
  </section>
</template>
