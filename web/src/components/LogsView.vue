<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  AlertTriangle, Bug, Check, ChevronDown, ChevronsDown, Copy, FileWarning, FilterX,
  GitFork, MessageSquareCode, RefreshCw, ScrollText, Search, SearchX, Trash2,
} from 'lucide-vue-next';
import ConfirmModal from './ConfirmModal.vue';
import type { DeveloperLogEvent, DeveloperLogType, DeveloperLogsResponse } from '../types';
import { makeRequest } from '../ui/makeRequest';
import { toastMessage } from '../composables/useToasts';

type Severity = DeveloperLogEvent['severity'];

const events = ref<DeveloperLogEvent[]>([]);
const loading = ref(false);
const error = toastMessage('error');
const search = ref('');
const type = ref<DeveloperLogType | ''>('');
const severity = ref<Severity | ''>('');
const from = ref('');
const to = ref('');
const offset = ref(0);
const total = ref(0);
const hasMore = ref(false);
const deletionMode = ref<'soft' | 'hard'>('soft');
const autoRefresh = ref(false);
const expanded = ref(new Set<string>());
const clearTarget = ref<DeveloperLogType | 'all' | null>(null);
const savingMode = ref(false);
const menuOpen = ref(false);
const copiedId = ref('');
const searchInput = ref<HTMLInputElement | null>(null);
const menuRoot = ref<HTMLElement | null>(null);
let refreshTimer: ReturnType<typeof setInterval> | null = null;
let debounceTimer: ReturnType<typeof setTimeout> | null = null;
let copiedTimer: ReturnType<typeof setTimeout> | null = null;

const labels: Record<DeveloperLogType, string> = { llm_prompt: 'LLM prompt', exception: 'Exception', router_fallback: 'Router fallback' };
const typeOptions: Array<{ value: DeveloperLogType; label: string; icon: typeof Bug }> = [
  { value: 'llm_prompt', label: 'LLM prompts', icon: MessageSquareCode },
  { value: 'exception', label: 'Exceptions', icon: Bug },
  { value: 'router_fallback', label: 'Router fallbacks', icon: GitFork },
];
const typeIcon: Record<DeveloperLogType, typeof Bug> = { llm_prompt: MessageSquareCode, exception: Bug, router_fallback: GitFork };
const severityOptions: Array<{ value: Severity; label: string }> = [
  { value: 'info', label: 'Info' },
  { value: 'warning', label: 'Warning' },
  { value: 'error', label: 'Error' },
];

const load = async (append = false) => {
  if (loading.value) return;
  loading.value = true;
  error.value = '';
  const query = new URLSearchParams({ offset: String(append ? offset.value : 0), limit: '50' });
  if (search.value.trim()) query.set('search', search.value.trim());
  if (type.value) query.set('types', type.value);
  if (severity.value) query.set('severity', severity.value);
  if (from.value) query.set('start', new Date(from.value).toISOString());
  if (to.value) query.set('end', new Date(to.value).toISOString());
  try {
    const response = await makeRequest(`/api/logs?${query}`);
    const data = await response.json() as DeveloperLogsResponse & { detail?: string };
    if (!response.ok) throw new Error(data.detail || 'Logs unavailable.');
    events.value = append ? [...events.value, ...data.events] : data.events;
    total.value = data.total;
    offset.value = events.value.length;
    hasMore.value = data.has_more;
    deletionMode.value = data.deletion_mode || 'soft';
  } catch (cause: any) {
    error.value = cause?.message || 'Logs unavailable.';
  } finally {
    loading.value = false;
  }
};

watch([search, type, severity, from, to], () => {
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => load(false), 300);
});
watch(autoRefresh, (enabled) => {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = enabled ? setInterval(() => load(false), 10000) : null;
});

const hasFilters = computed(() => Boolean(search.value.trim() || type.value || severity.value || from.value || to.value));
const resetFilters = () => { search.value = ''; type.value = ''; severity.value = ''; from.value = ''; to.value = ''; };

const toggle = (id: string) => {
  const next = new Set(expanded.value);
  if (next.has(id)) next.delete(id); else next.add(id);
  expanded.value = next;
};

const askClear = (target: DeveloperLogType | 'all') => { menuOpen.value = false; clearTarget.value = target; };
const clearConfirmed = async () => {
  if (!clearTarget.value) return;
  const target = clearTarget.value;
  const response = await makeRequest(target === 'all' ? '/api/logs' : `/api/logs/${target}`, { method: 'DELETE' });
  if (!response.ok) { error.value = 'Failed to clear logs.'; return; }
  clearTarget.value = null;
  await load(false);
};

const setMode = async (mode: 'soft' | 'hard') => {
  if (mode === deletionMode.value || savingMode.value) return;
  const previous = deletionMode.value;
  deletionMode.value = mode;
  savingMode.value = true;
  try {
    const response = await makeRequest('/api/logs/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ deletion_mode: mode }) });
    if (!response.ok) throw new Error('Failed to save clear mode.');
  } catch (cause: any) {
    deletionMode.value = previous;
    error.value = cause?.message || 'Failed to save clear mode.';
  } finally {
    savingMode.value = false;
  }
};

const clearMessage = computed(() => `This will ${deletionMode.value === 'hard' ? 'remove the active log and rotated backups' : 'truncate only the active log'}${clearTarget.value === 'all' ? ' for all log types' : ` for ${labels[clearTarget.value as DeveloperLogType]}`}.`);

// Events arrive newest first; group them under a heading per local day.
const dayKey = (value: string) => new Date(value).toDateString();
const dayLabel = (value: string) => {
  const date = new Date(value);
  const today = new Date();
  const yesterday = new Date(); yesterday.setDate(today.getDate() - 1);
  const named = date.toDateString() === today.toDateString() ? 'Today' : date.toDateString() === yesterday.toDateString() ? 'Yesterday' : '';
  const full = date.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: date.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
  return { named, full };
};
const groups = computed(() => {
  const result: Array<{ key: string; label: { named: string; full: string }; events: DeveloperLogEvent[] }> = [];
  for (const event of events.value) {
    const key = dayKey(event.timestamp);
    const last = result[result.length - 1];
    if (last && last.key === key) last.events.push(event);
    else result.push({ key, label: dayLabel(event.timestamp), events: [event] });
  }
  return result;
});
const formatTime = (value: string) => new Date(value).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const formatStamp = (value: string) => new Date(value).toLocaleString();

const routerFields = (event: DeveloperLogEvent) => {
  const d = event.details;
  return [
    d.route ? { label: 'route', value: d.route } : null,
    d.finalChoice ? { label: 'final choice', value: d.finalChoice } : null,
  ].filter((field): field is { label: string; value: string } => Boolean(field));
};
const hasConfidence = (event: DeveloperLogEvent) => typeof event.details.confidence === 'number' && Number.isFinite(event.details.confidence);
const confidencePercent = (event: DeveloperLogEvent) => `${Math.round(Math.min(1, Math.max(0, event.details.confidence ?? 0)) * 100)}%`;
const metadataText = (event: DeveloperLogEvent) => {
  const metadata = event.details.metadata;
  return metadata && Object.keys(metadata).length ? JSON.stringify(metadata, null, 2) : '';
};
const detailText = (event: DeveloperLogEvent) => {
  if (event.type === 'llm_prompt') return event.details.prompt || '';
  if (event.type === 'exception') return event.details.stackTrace || event.details.message || '';
  return metadataText(event);
};
const detailLabel = (event: DeveloperLogEvent) => event.type === 'llm_prompt' ? 'prompt' : event.type === 'exception' ? (event.details.stackTrace ? 'stack trace' : 'message') : 'metadata';

// Stack traces: mark the error line and the file paths without injecting HTML.
const traceLines = (text: string) => text.split('\n').map((line, index) => {
  const match = /^(\s*File ")([^"]+)(".*)$/.exec(line);
  if (match) return { key: index, parts: [{ text: match[1], tone: '' }, { text: match[2], tone: 'file' }, { text: match[3], tone: '' }] };
  const isError = /^\S.*(Error|Exception|Response)\b.*:/.test(line) || /^\S+Error\b/.test(line);
  return { key: index, parts: [{ text: line, tone: isError ? 'error' : '' }] };
});

const copy = async (event: DeveloperLogEvent) => {
  try {
    await navigator.clipboard.writeText(detailText(event));
    copiedId.value = event.id;
    if (copiedTimer) clearTimeout(copiedTimer);
    copiedTimer = setTimeout(() => { copiedId.value = ''; }, 1600);
  } catch {
    error.value = 'Copy failed. Select the text and copy it instead.';
  }
};

const onKeydown = (e: KeyboardEvent) => {
  if (e.key === 'Escape' && menuOpen.value) { menuOpen.value = false; return; }
  const target = e.target as HTMLElement | null;
  const typing = target && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName));
  if (e.key === '/' && !typing && !e.metaKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); searchInput.value?.focus(); }
};
const onPointerDown = (e: PointerEvent) => {
  if (menuOpen.value && menuRoot.value && !menuRoot.value.contains(e.target as Node)) menuOpen.value = false;
};

onMounted(() => {
  load();
  window.addEventListener('keydown', onKeydown);
  document.addEventListener('pointerdown', onPointerDown);
});
onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer);
  if (debounceTimer) clearTimeout(debounceTimer);
  if (copiedTimer) clearTimeout(copiedTimer);
  window.removeEventListener('keydown', onKeydown);
  document.removeEventListener('pointerdown', onPointerDown);
});
</script>

<template>
  <div class="lg-page">
    <div class="lg-wrap">
      <div class="lg-head">
        <div class="lg-title">
          <h2><ScrollText />Developer logs</h2>
          <p><b>{{ total }}</b> matching event{{ total === 1 ? '' : 's' }}, newest first</p>
        </div>
        <div class="lg-actions">
          <button type="button" class="lg-btn lg-live" :aria-pressed="autoRefresh" title="Refresh every 10 seconds" @click="autoRefresh = !autoRefresh">
            <span class="lg-live-dot" aria-hidden="true"></span>Live · 10s
          </button>
          <button type="button" class="lg-btn" :disabled="loading" @click="load(false)">
            <RefreshCw :class="loading ? 'lg-spin' : ''" />Refresh
          </button>
          <div ref="menuRoot" class="lg-menu-root">
            <button type="button" class="lg-btn" data-variant="danger" :aria-expanded="menuOpen" aria-haspopup="true" @click="menuOpen = !menuOpen">
              <Trash2 />Clear<ChevronDown class="lg-chev" />
            </button>
            <div v-if="menuOpen" class="lg-menu">
              <p class="lg-menu-title">clear one type</p>
              <button v-for="option in typeOptions" :key="option.value" type="button" class="lg-menu-item" @click="askClear(option.value)">
                <i class="lg-type-dot" :data-type="option.value"></i>{{ option.label }}
              </button>
              <button type="button" class="lg-menu-item" data-variant="danger" @click="askClear('all')"><Trash2 />Clear all logs</button>
              <p class="lg-menu-title">clear mode</p>
              <div class="lg-modes" role="group" aria-label="Clear mode">
                <button type="button" class="lg-mode" :aria-pressed="deletionMode === 'soft'" :disabled="savingMode" @click="setMode('soft')">
                  <b>Soft</b><span>Empties the active file only</span>
                </button>
                <button type="button" class="lg-mode" :aria-pressed="deletionMode === 'hard'" :disabled="savingMode" @click="setMode('hard')">
                  <b>Hard</b><span>Also removes rotated backups</span>
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="lg-filters">
        <label class="lg-search">
          <Search />
          <input ref="searchInput" v-model="search" type="search" placeholder="Search titles, prompts, stack traces…" aria-label="Search logs" />
          <kbd aria-hidden="true">/</kbd>
        </label>
        <div class="lg-frow">
          <span class="lg-flabel">type</span>
          <div class="lg-chips" role="group" aria-label="Log type">
            <button type="button" class="lg-chip" :aria-pressed="type === ''" @click="type = ''">All</button>
            <button v-for="option in typeOptions" :key="option.value" type="button" class="lg-chip" :aria-pressed="type === option.value" @click="type = option.value">
              <i class="lg-type-dot" :data-type="option.value"></i>{{ option.label }}
            </button>
          </div>
          <span class="lg-sep" aria-hidden="true"></span>
          <span class="lg-flabel">severity</span>
          <div class="lg-chips" role="group" aria-label="Severity">
            <button type="button" class="lg-chip" :aria-pressed="severity === ''" @click="severity = ''">Any</button>
            <button v-for="option in severityOptions" :key="option.value" type="button" class="lg-chip" :aria-pressed="severity === option.value" @click="severity = option.value">{{ option.label }}</button>
          </div>
        </div>
        <div class="lg-frow">
          <span class="lg-flabel">time</span>
          <input v-model="from" type="datetime-local" class="lg-date" aria-label="From" />
          <span class="lg-flabel" aria-hidden="true">→</span>
          <input v-model="to" type="datetime-local" class="lg-date" aria-label="To" />
          <button v-if="from || to" type="button" class="lg-link" @click="from = ''; to = ''">Clear range</button>
        </div>
      </div>

      <div v-if="!events.length && loading" class="lg-stream lg-empty">
        <RefreshCw class="lg-spin lg-empty-spin" />
        <span>Loading logs…</span>
      </div>
      <div v-else-if="!events.length" class="lg-stream lg-empty">
        <span class="lg-empty-icon"><SearchX /></span>
        <b>{{ hasFilters ? 'No events match these filters' : 'No log events yet' }}</b>
        <span>{{ hasFilters ? 'Try another type, severity or time range.' : 'Prompts, exceptions and router fallbacks will appear here.' }}</span>
        <button v-if="hasFilters" type="button" class="lg-btn lg-btn-sm" @click="resetFilters"><FilterX />Reset filters</button>
      </div>

      <div v-else class="lg-stream">
        <template v-for="group in groups" :key="group.key">
          <div class="lg-day"><b v-if="group.label.named">{{ group.label.named }}</b><span>{{ group.label.named ? ' · ' : '' }}{{ group.label.full }}</span></div>
          <article v-for="event in group.events" :key="event.id" class="lg-event" :data-open="expanded.has(event.id)">
            <button type="button" class="lg-row" :data-severity="event.severity" :aria-expanded="expanded.has(event.id)" @click="toggle(event.id)">
              <time :datetime="event.timestamp" :title="formatStamp(event.timestamp)">{{ formatTime(event.timestamp) }}</time>
              <span class="lg-tag" :data-type="event.type"><component :is="typeIcon[event.type]" />{{ labels[event.type] }}</span>
              <span class="lg-row-text"><b>{{ event.title }}</b><span>{{ event.summary }}</span></span>
              <ChevronDown class="lg-chev" />
            </button>
            <div v-if="expanded.has(event.id)" class="lg-body">
              <p v-if="event.details.query" class="lg-query"><b>query</b>{{ event.details.query }}</p>
              <p v-if="event.type === 'exception' && event.details.stackTrace && event.details.message" class="lg-query"><b>message</b>{{ event.details.message }}</p>

              <dl v-if="event.type === 'router_fallback'" class="lg-kv">
                <div v-for="field in routerFields(event)" :key="field.label"><dt>{{ field.label }}</dt><dd>{{ field.value }}</dd></div>
                <div v-if="hasConfidence(event)"><dt>confidence</dt><dd class="lg-conf">{{ event.details.confidence?.toFixed(2) }}<i :style="{ '--lg-conf': confidencePercent(event) }"></i></dd></div>
                <div v-if="event.details.reason" class="lg-kv-wide"><dt>reason</dt><dd>{{ event.details.reason }}</dd></div>
              </dl>

              <div v-if="detailText(event)" class="lg-code">
                <div class="lg-code-head">
                  <FileWarning v-if="event.type === 'exception'" /><MessageSquareCode v-else />
                  {{ detailLabel(event) }}
                  <button type="button" class="lg-btn lg-btn-sm" @click="copy(event)">
                    <Check v-if="copiedId === event.id" /><Copy v-else />{{ copiedId === event.id ? 'Copied' : 'Copy' }}
                  </button>
                </div>
                <pre v-if="event.type === 'exception'"><span v-for="line in traceLines(detailText(event))" :key="line.key" class="lg-line"><span v-for="(part, i) in line.parts" :key="i" :data-tone="part.tone || undefined">{{ part.text }}</span>
</span></pre>
                <pre v-else>{{ detailText(event) }}</pre>
              </div>

              <p v-if="event.details.parserWarning" class="lg-warn"><AlertTriangle />{{ event.details.parserWarning }}</p>
            </div>
          </article>
        </template>
        <div class="lg-more">
          <span>Showing {{ events.length }} of {{ total }}</span>
          <button v-if="hasMore" type="button" class="lg-btn lg-btn-sm" :disabled="loading" @click="load(true)"><ChevronsDown />Load 50 more</button>
        </div>
      </div>
    </div>
    <ConfirmModal :show="!!clearTarget" title="Clear developer logs?" :message="clearMessage" confirm-label="Clear logs" @confirm="clearConfirmed" @cancel="clearTarget = null" />
  </div>
</template>
