<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { AlertCircle, ChevronLeft, ChevronRight, LoaderCircle, X } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';
import RuleLinksPanel from './RuleLinksPanel.vue';

type SourceLine = { number: number; text: string };
type SourceWindow = {
  file: string;
  requested_line: number;
  total_lines: number;
  start_line: number;
  end_line: number;
  has_previous: boolean;
  has_next: boolean;
  lines: SourceLine[];
};

const props = defineProps<{
  projectId: string;
  revisionId: string;
  commitSha: string | null;
  filePath: string;
  line: number;
}>();
const emit = defineEmits<{ close: [] }>();

const source = ref<SourceWindow | null>(null);
const loading = ref(false);
const error = ref('');
const latestRequest = ref(0);
const anchorNote = computed(() => props.line === 1
  ? 'The assessment reports line 1 as a file-level anchor; it does not identify a specific defective statement.'
  : `Showing the source around reported line ${props.line}.`);

const loadWindow = async (startLine?: number) => {
  const requestId = ++latestRequest.value;
  loading.value = true;
  error.value = '';
  try {
    const query = new URLSearchParams({ file_path: props.filePath, line: String(props.line) });
    if (startLine) query.set('start_line', String(startLine));
    const path = `/api/code-quality/projects/${encodeURIComponent(props.projectId)}/revisions/${encodeURIComponent(props.revisionId)}/source?${query.toString()}`;
    const response = await makeRequest(path);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || 'Source from this assessment revision is unavailable.');
    }
    const result = await response.json() as SourceWindow;
    if (requestId === latestRequest.value) source.value = result;
  } catch (cause: any) {
    if (requestId === latestRequest.value) error.value = cause?.message || 'Source from this assessment revision is unavailable.';
  } finally {
    if (requestId === latestRequest.value) loading.value = false;
  }
};

watch(() => [props.projectId, props.revisionId, props.filePath, props.line], () => {
  source.value = null;
  void loadWindow();
}, { immediate: true });
</script>

<template>
  <section class="rounded-xl border border-indigo-400/30 bg-slate-950/80 p-4" aria-label="Source from assessed revision" aria-live="polite">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div class="min-w-0">
        <h3 class="text-sm font-semibold text-slate-100">Source from assessed revision</h3>
        <p class="mt-1 break-all font-mono text-xs text-indigo-200">{{ filePath }}<span v-if="line">:{{ line }}</span></p>
        <p class="mt-1 text-xs text-slate-500">{{ commitSha ? `Commit ${commitSha.slice(0, 12)}` : `Upload revision ${revisionId}` }} · {{ source?.total_lines ?? '…' }} lines</p>
      </div>
      <button type="button" class="rounded-lg p-2 text-slate-400 hover:bg-slate-800 hover:text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-400" aria-label="Close source preview" @click="emit('close')"><X :size="16" /></button>
    </div>

    <p class="mt-3 text-xs text-slate-400">{{ anchorNote }}</p>
    <div v-if="error" class="mt-3 flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200" role="alert">
      <AlertCircle class="mt-0.5 shrink-0" :size="16" /><span class="min-w-0 flex-1">{{ error }}</span>
      <button type="button" class="shrink-0 underline underline-offset-2" @click="loadWindow(source?.start_line)">Retry</button>
    </div>
    <div v-else-if="loading && !source" class="mt-3 flex items-center gap-2 text-sm text-slate-400" role="status"><LoaderCircle class="motion-safe:animate-spin" :size="15" />Loading revision source…</div>
    <div v-else-if="source" class="mt-3 overflow-hidden rounded-lg border border-slate-800">
      <div class="max-h-[28rem] overflow-auto bg-slate-950 py-2 font-mono text-xs leading-5">
        <div v-for="item in source.lines" :key="item.number" class="flex min-w-max" :class="item.number === line ? 'bg-indigo-400/15 text-indigo-50' : 'text-slate-300'">
          <span class="sticky left-0 w-14 shrink-0 select-none bg-slate-950/95 px-3 text-right text-slate-600">{{ item.number }}</span>
          <span class="whitespace-pre px-3">{{ item.text || ' ' }}</span>
        </div>
      </div>
      <div class="flex items-center justify-between border-t border-slate-800 px-3 py-2 text-xs text-slate-500">
        <button type="button" class="inline-flex items-center gap-1 rounded px-2 py-1 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40" :disabled="!source.has_previous || loading" @click="loadWindow(Math.max(1, source.start_line - 100))"><ChevronLeft :size="14" />Previous</button>
        <span>Lines {{ source.start_line }}–{{ source.end_line }} of {{ source.total_lines }}</span>
        <button type="button" class="inline-flex items-center gap-1 rounded px-2 py-1 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40" :disabled="!source.has_next || loading" @click="loadWindow(source.start_line + 100)">Next<ChevronRight :size="14" /></button>
      </div>
    </div>
    <RuleLinksPanel class="mt-3" compact :project-id="projectId" :revision-id="revisionId" :file="filePath" />
  </section>
</template>
