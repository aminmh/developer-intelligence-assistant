<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import {
  FolderGit2,
  Trash2,
  Cpu,
  RefreshCw,
  Sparkles,
  Check,
  CircleX,
  LoaderCircle,
  Upload,
  GitCommitHorizontal,
  Container,
  TriangleAlert,
  Library,
  UploadCloud,
  GitBranch,
  Archive,
} from 'lucide-vue-next';
import type { UploadedFile, RuntimeProjectRecord } from '../types';
import { makeRequest } from '../ui/makeRequest';
import ConfirmModal from './ConfirmModal.vue';
import { useProjectScope } from '../composables/useProjectScope';
import { fileIndexStatus } from '../utils/fileIndexStatus';

const props = defineProps<{
  files: UploadedFile[];
  parsingFileId: string | null;
  indexingProgress: { total: number; indexed: number; status: string; stage?: string; error?: string; graph_status?: string; graph_index_complete?: boolean; graph_nodes?: number; graph_relations?: number };
  indexedFileIds: Set<string>;
  embeddingProvider: 'ollama' | 'openAICompatible' | 'cohere' | 'cloudflareWorkersAI';
}>();

const emit = defineEmits<{
  (e: 'deleteFile', id: string, name: string): void;
  (e: 'indexFile', id: string, name: string): void;
  (e: 'rebuildCohereEmbeddings', id: string, name: string): void;
  (e: 'chooseUpload'): void;
  (e: 'connectGit'): void;
}>();

const showDeleteConfirm = ref(false);
const selectedFile = ref<UploadedFile | null>(null);
// The last revision the user started indexing: its error message stays visible on that row.
const lastIndexedId = ref<string | null>(null);
const runtimeProjects = ref<RuntimeProjectRecord[]>([]);
const runtimeLoadError = ref('');
const scope = useProjectScope();

const progressStageMessage: Record<string, string> = {
  queued: 'Queueing Cohere rebuild…',
  extracting: 'Preparing source revision…',
  embedding: 'Embedding project chunks…',
  graph_indexing: 'Checking Neo4j and writing the project graph…',
  upserting: 'Saving embeddings…',
  activating: 'Activating new embeddings…',
};

// ---- Runtime deployment status -------------------------------------------
const deploymentByScope = computed(() => new Map(runtimeProjects.value
  .filter((row) => row.commit_sha)
  .map((row) => [`${row.project_id}:${row.commit_sha}`, row])));
const runningByProject = computed(() => {
  const map = new Map<string, number>();
  for (const row of runtimeProjects.value) {
    if (row.running_project_containers) map.set(row.project_id, Math.max(map.get(row.project_id) ?? 0, row.running_project_containers));
  }
  return map;
});
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

// ---- Grouping -------------------------------------------------------------
const LEGACY = '__legacy';
const groups = computed(() => {
  const byProject = new Map<string, UploadedFile[]>();
  for (const file of props.files) {
    const key = file.projectId || LEGACY;
    byProject.set(key, [...(byProject.get(key) ?? []), file]);
  }
  return [...byProject.entries()]
    .map(([key, files]) => ({
      key,
      name: key === LEGACY ? 'Legacy uploads' : key,
      legacy: key === LEGACY,
      files: [...files].sort((a, b) => Date.parse(b.uploadedAt) - Date.parse(a.uploadedAt)),
      latest: Math.max(...files.map((file) => Date.parse(file.uploadedAt) || 0)),
    }))
    .sort((a, b) => (a.legacy ? 1 : b.legacy ? -1 : b.latest - a.latest));
});

// ---- Row state -------------------------------------------------------------
type RowState = 'indexing' | 'processing' | 'failed' | 'indexed' | 'pending' | 'unknown';
const stateOf = (file: UploadedFile): RowState => {
  if (props.parsingFileId === file.id) return 'indexing';
  if (file.status === 'processing') return 'processing';
  if (file.status === 'failed') return 'failed';
  return fileIndexStatus(file, scope.projects.value, scope.loaded.value && !scope.loadError.value);
};
const canRebuildCohere = (file: UploadedFile) => props.embeddingProvider === 'cohere' && !!file.projectId
  && !!(file.commitSha || file.revisionId) && !!file.uploadSessionId && props.parsingFileId !== file.id;

const relativeTime = (iso: string | number) => {
  const time = typeof iso === 'number' ? iso : Date.parse(iso);
  if (!Number.isFinite(time) || time <= 0) return '';
  const minutes = Math.round((Date.now() - time) / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days === 1) return 'yesterday';
  if (days < 14) return `${days} days ago`;
  return new Date(time).toLocaleDateString();
};
const formatSize = (bytes: number) => (bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${(bytes / 1024).toFixed(1)} KB`);

const indexingPercent = computed(() => (props.indexingProgress.total > 0
  ? Math.min(100, Math.round((props.indexingProgress.indexed / props.indexingProgress.total) * 100)) : 0));
const indexingLabel = computed(() => {
  const stage = props.indexingProgress.stage || props.indexingProgress.status;
  if (props.indexingProgress.total > 0) {
    if (stage === 'upserting') return 'Saving embeddings…';
    if (stage === 'activating') return 'Activating Cohere embeddings…';
    if (stage === 'domain_embedding') return `Embedding domain concepts: ${props.indexingProgress.indexed}/${props.indexingProgress.total}`;
    return `Embedding chunks: ${props.indexingProgress.indexed.toLocaleString()} / ${props.indexingProgress.total.toLocaleString()}`;
  }
  return progressStageMessage[stage] || 'Extracting PHP code…';
});

const startIndex = (file: UploadedFile) => {
  lastIndexedId.value = file.id;
  emit('indexFile', file.id, file.name);
};

const confirmDelete = (file: UploadedFile) => {
  selectedFile.value = file;
  showDeleteConfirm.value = true;
};
const handleDeleteConfirm = () => {
  if (selectedFile.value) emit('deleteFile', selectedFile.value.id, selectedFile.value.name);
  showDeleteConfirm.value = false;
  selectedFile.value = null;
};
const handleDeleteCancel = () => {
  showDeleteConfirm.value = false;
  selectedFile.value = null;
};
</script>

<template>
  <div class="kb-projects">
    <div v-if="files.length === 0" class="kb-empty">
      <span class="kb-empty-icon"><Library aria-hidden="true" /></span>
      <h3>Add your first project</h3>
      <p>Upload a ZIP of your source code, or connect a GitHub branch. Once it's indexed, chat and code quality can answer questions about it.</p>
      <div class="kb-empty-actions">
        <button type="button" class="kb-btn" data-variant="primary" @click="emit('chooseUpload')"><UploadCloud aria-hidden="true" />Upload a ZIP</button>
        <button type="button" class="kb-btn" @click="emit('connectGit')"><GitBranch aria-hidden="true" />Connect GitHub</button>
      </div>
    </div>

    <article v-for="group in groups" :key="group.key" class="kb-project" :aria-label="`Project ${group.name}`">
      <header class="kb-project-head">
        <span class="kb-project-icon"><Archive v-if="group.legacy" aria-hidden="true" /><FolderGit2 v-else aria-hidden="true" /></span>
        <div class="min-w-0">
          <h3 class="kb-project-name">{{ group.name }}</h3>
          <p class="kb-project-meta">
            {{ group.files.length }} {{ group.files.length === 1 ? 'revision' : 'revisions' }}<template v-if="group.latest"> · latest {{ relativeTime(group.latest) }}</template>
            <template v-if="group.legacy"> · no project attribution</template>
          </p>
        </div>
        <span v-if="!group.legacy && runningByProject.get(group.key)" class="kb-tag" data-tone="ok">
          <Container aria-hidden="true" />Running in Docker · {{ runningByProject.get(group.key) }} {{ runningByProject.get(group.key) === 1 ? 'container' : 'containers' }}
        </span>
      </header>

      <div v-for="file in group.files" :key="file.id" class="kb-rev" :data-state="stateOf(file)">
        <span class="kb-rev-dot" aria-hidden="true"></span>
        <div class="min-w-0">
          <div class="kb-rev-title">
            <b class="truncate" :title="file.name">{{ file.name }}</b>
            <span v-if="stateOf(file) === 'indexing'" class="kb-tag" data-tone="run"><LoaderCircle class="kb-spin" aria-hidden="true" />Indexing</span>
            <span v-else-if="stateOf(file) === 'processing'" class="kb-tag" data-tone="run"><LoaderCircle class="kb-spin" aria-hidden="true" />Processing</span>
            <span v-else-if="stateOf(file) === 'failed'" class="kb-tag" data-tone="bad"><CircleX aria-hidden="true" />Failed</span>
            <span v-else-if="stateOf(file) === 'indexed'" class="kb-tag" data-tone="ok">
              <Check aria-hidden="true" />Indexed<template v-if="file.chunkCount > 0"> · {{ file.chunkCount.toLocaleString() }} {{ file.chunkCount === 1 ? 'chunk' : 'chunks' }}</template>
            </span>
            <span v-else-if="stateOf(file) === 'unknown'" class="kb-tag" data-tone="warn">Index status unavailable</span>
            <span v-else class="kb-tag" data-tone="warn">Not indexed</span>
            <span v-if="file.revisionId" class="kb-tag" :title="file.archiveSha256 ? `Archive SHA-256 ${file.archiveSha256}` : file.revisionId">{{ file.revisionId }}</span>
            <span v-else-if="file.projectId" class="kb-tag" data-tone="dim">Revision registration pending</span>
          </div>

          <div class="kb-rev-sub">
            <span v-if="file.commitSha"><GitCommitHorizontal aria-hidden="true" />Git commit {{ file.commitSha }}</span>
            <span v-else-if="file.projectId"><Upload aria-hidden="true" />Upload revision · no Git commit required</span>
            <span>{{ file.type.toUpperCase() }} · {{ formatSize(file.size) }}</span>
            <span v-if="file.uploadedAt">uploaded {{ relativeTime(file.uploadedAt) }}</span>
          </div>

          <p v-if="file.projectId && file.commitSha" class="kb-rev-note" :data-tone="runtimeFor(file)?.deployment_mismatch ? 'warn' : runtimeFor(file)?.running_project_containers ? 'ok' : undefined">
            <TriangleAlert v-if="runtimeFor(file)?.deployment_mismatch" aria-hidden="true" />
            <Container v-else aria-hidden="true" />
            {{ runtimeFor(file)?.deployment_mismatch ? 'The running deployment uses a different commit' : runtimeFor(file)?.running_project_containers ? `Matches the running deployment · ${runtimeFor(file)?.running_project_containers} ${runtimeFor(file)?.running_project_containers === 1 ? 'container' : 'containers'}` : runtimeLoadError ? 'Runtime deployment status unavailable' : 'No running Docker deployment reported' }}
          </p>
          <p v-if="file.status === 'failed'" class="kb-rev-note" data-tone="bad" :title="file.error">
            <TriangleAlert aria-hidden="true" />{{ file.error || 'Indexing failed.' }}
          </p>

          <div v-if="parsingFileId === file.id" class="kb-indexing">
            <div class="kb-progress-top" aria-live="polite">
              <span>{{ indexingLabel }}</span>
              <span v-if="indexingProgress.total > 0">{{ indexingPercent }}%</span>
            </div>
            <div
              v-if="indexingProgress.total > 0"
              class="kb-track"
              role="progressbar"
              :aria-valuenow="indexingPercent"
              aria-valuemin="0"
              aria-valuemax="100"
              :aria-label="indexingLabel"
            ><i :style="{ transform: `scaleX(${indexingPercent / 100})` }"></i></div>
            <div v-else class="kb-track" data-indeterminate="true"></div>
            <p v-if="indexingProgress.graph_status" class="kb-graph" aria-live="polite">
              Neo4j graph: {{ indexingProgress.graph_status === 'ready' && indexingProgress.graph_index_complete ? `complete (${indexingProgress.graph_nodes ?? 0} symbols, ${indexingProgress.graph_relations ?? 0} relationships)` : indexingProgress.graph_status }}
            </p>
          </div>
          <p v-if="indexingProgress.status === 'error' && lastIndexedId === file.id && parsingFileId !== file.id" role="alert" class="kb-rev-note" data-tone="bad">
            <TriangleAlert aria-hidden="true" />{{ indexingProgress.error || 'Embedding failed. Check the API service and retry.' }}
          </p>
        </div>

        <div class="kb-actions">
          <template v-if="parsingFileId !== file.id && file.status !== 'processing'">
            <button v-if="stateOf(file) === 'failed'" type="button" class="kb-btn" data-variant="primary" @click="startIndex(file)"><Cpu aria-hidden="true" />Retry index</button>
            <button v-else-if="stateOf(file) === 'pending'" type="button" class="kb-btn" data-variant="primary" @click="startIndex(file)"><Cpu aria-hidden="true" />Index</button>
            <button v-else-if="stateOf(file) === 'indexed'" type="button" class="kb-btn" @click="startIndex(file)"><RefreshCw aria-hidden="true" />Re-index</button>
          </template>
          <button v-if="canRebuildCohere(file)" type="button" class="kb-btn" @click="emit('rebuildCohereEmbeddings', file.id, file.name)"><Sparkles aria-hidden="true" />Rebuild Cohere</button>
          <button type="button" class="kb-btn kb-btn-icon" data-variant="danger" :aria-label="`Delete ${file.name}`" :title="`Delete ${file.name}`" @click="confirmDelete(file)">
            <Trash2 aria-hidden="true" />
          </button>
        </div>
      </div>
    </article>

    <ConfirmModal
      :show="showDeleteConfirm"
      title="Delete revision"
      :message="`Delete ${selectedFile?.name ?? ''}? This can't be undone.`"
      @confirm="handleDeleteConfirm"
      @cancel="handleDeleteCancel"
    />
  </div>
</template>
