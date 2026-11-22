<script setup lang="ts">
/**
 * For one consumer attachment: how the project's requests and models agree with the contract,
 * and what a contract change touches. Spec: .archcore/frontend-assist/contract-conformance-findings.spec.md
 */
import { computed, ref, watch } from 'vue';
import { CircleAlert, CircleCheck, Info, LoaderCircle, TriangleAlert } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';
import { useProjectScope } from '../../composables/useProjectScope';
import { toast } from '../../composables/useToasts';
import { versionLabel, type ContractVersion } from '../../utils/apiContracts';
import {
  changeLabel, findingGroups, severityLabel,
  type ConformanceReport, type ImpactReport, type Severity,
} from '../../utils/contractCheck';

const props = defineProps<{ projectId: string; attachmentId: string; role: 'consumer' | 'provider' }>();
const scope = useProjectScope();
type Tab = 'findings' | 'impact' | 'runtime';
const tab = ref<Tab>('findings');
const report = ref<ConformanceReport | null>(null);
const live = ref<ConformanceReport | null>(null);
const impact = ref<ImpactReport | null>(null);
const loading = ref(false);
const error = ref('');
const shown = ref<Record<Severity, boolean>>({ error: true, warning: true, info: false });
const fromVersion = ref('');
const toVersion = ref('');
const showUnused = ref(false);

const base = computed(() => `/api/projects/${encodeURIComponent(props.projectId)}/contracts/attachments/${props.attachmentId}`);
const revisionQuery = () => {
  const query = new URLSearchParams();
  const revision = scope.projectId.value.trim() === props.projectId ? scope.resolvedRevision.value?.revision_id : '';
  if (revision) query.set('revision_id', revision);
  return query;
};
let sequence = 0;
const run = async (work: () => Promise<void>) => {
  const current = ++sequence;
  loading.value = true; error.value = '';
  try { await work(); }
  catch (cause: any) { if (current === sequence) { error.value = cause?.message || 'The contract check is unavailable.'; toast.error(error.value); } }
  finally { if (current === sequence) loading.value = false; }
};
const read = async (path: string, query: URLSearchParams) => {
  const response = await makeRequest(`${base.value}/${path}?${query}`);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The contract check is unavailable.');
  return data;
};
const loadFindings = () => run(async () => { report.value = await read('conformance', revisionQuery()); });
const loadLive = () => run(async () => { live.value = await read('runtime', revisionQuery()); });
const loadImpact = () => run(async () => {
  const query = revisionQuery();
  if (fromVersion.value) query.set('from_version', fromVersion.value);
  if (toVersion.value) query.set('to_version', toVersion.value);
  impact.value = await read('impact', query);
  fromVersion.value = impact.value?.from_version?.version_id || fromVersion.value;
  toVersion.value = impact.value?.to_version?.version_id || toVersion.value;
});
watch(() => [props.attachmentId, scope.resolvedRevision.value?.revision_id] as const, () => {
  report.value = null; impact.value = null; live.value = null; fromVersion.value = ''; toVersion.value = '';
  void (tab.value === 'findings' ? loadFindings() : tab.value === 'runtime' ? loadLive() : loadImpact());
}, { immediate: true });
const open = (next: Tab) => {
  tab.value = next;
  if (next === 'findings' && !report.value) void loadFindings();
  if (next === 'impact' && !impact.value) void loadImpact();
  if (next === 'runtime' && !live.value) void loadLive();
};
const groups = computed(() => findingGroups(report.value?.findings || []));
const liveGroups = computed(() => findingGroups(live.value?.findings || []));
const versions = computed<ContractVersion[]>(() => impact.value?.versions || []);
const icons = { error: CircleAlert, warning: TriangleAlert, info: Info };
</script>

<template>
  <div class="cc-panel">
    <div class="kb-tabs" role="tablist" aria-label="Contract check">
      <button type="button" role="tab" :aria-selected="tab === 'findings'" @click="open('findings')">{{ role === 'provider' ? 'Routes' : 'Requests and models' }}</button>
      <button v-if="role === 'consumer'" type="button" role="tab" :aria-selected="tab === 'impact'" @click="open('impact')">Change impact</button>
      <button v-if="role === 'provider'" type="button" role="tab" :aria-selected="tab === 'runtime'" @click="open('runtime')">Live traffic</button>
    </div>
    <p v-if="loading" class="kb-note" role="status"><LoaderCircle class="kb-spin" aria-hidden="true" /> Checking the project against the contract…</p>
    <template v-else-if="error" />

    <template v-else-if="tab === 'findings' && report">
      <p v-if="!report.available" class="kb-rev-note" data-tone="warn"><CircleAlert aria-hidden="true" />{{ report.notes.join(' ') }}</p>
      <template v-else>
        <div v-if="report.check === 'provider'" class="cc-summary">
          <span class="kb-stat"><b>{{ report.coverage.routes_documented }}/{{ report.coverage.routes_in_scope }}</b>API routes are in the contract</span>
          <span class="kb-stat"><b>{{ report.coverage.operations_implemented }}/{{ report.coverage.operations_total }}</b>operations have a route</span>
          <span class="kb-stat"><b>{{ report.coverage.routes_total }}</b>routes in the project</span>
          <span class="cc-against">against {{ versionLabel(report.version) }}</span>
        </div>
        <p v-if="report.check === 'consumer' && !report.coverage.requests_total" class="kb-rev-note" data-tone="warn"><CircleAlert aria-hidden="true" />No HTTP requests were found in this project's indexed revision.</p>
        <div v-if="report.check === 'consumer'" class="cc-summary">
          <span class="kb-stat"><b>{{ report.coverage.requests_matched }}/{{ report.coverage.requests_total }}</b>requests match an operation</span>
          <span class="kb-stat"><b>{{ report.coverage.operations_used }}/{{ report.coverage.operations_total }}</b>operations used</span>
          <span v-if="report.coverage.requests_unresolved" class="kb-stat"><b>{{ report.coverage.requests_unresolved }}</b>not checkable</span>
          <span class="cc-against">against {{ versionLabel(report.version) }}</span>
        </div>
        <p v-for="note in report.notes" :key="note" class="kb-note">{{ note }}</p>
        <p v-if="!report.findings.length" class="kb-rev-note" data-tone="ok"><CircleCheck aria-hidden="true" />{{ report.check === 'provider' ? 'Every API route is in the contract, and every operation has a route.' : 'Every request that could be checked agrees with the contract.' }}</p>

        <section v-for="group in groups" :key="group.severity" class="cc-group">
          <button type="button" class="cc-group-head" :data-tone="group.severity" :aria-expanded="shown[group.severity]" @click="shown[group.severity] = !shown[group.severity]">
            <component :is="icons[group.severity]" aria-hidden="true" />{{ group.findings.length }} {{ severityLabel(group.severity, group.findings.length) }}
            <span>{{ shown[group.severity] ? 'Hide' : 'Show' }}</span>
          </button>
          <ul v-if="shown[group.severity]" class="cc-findings">
            <li v-for="finding in group.findings" :key="finding.finding_id">
              <span>{{ finding.message }}</span>
              <span v-if="finding.location.file" class="cc-where">{{ finding.location.file }}<template v-if="finding.location.line">:{{ finding.location.line }}</template><template v-if="finding.location.symbol"> · {{ finding.location.symbol }}</template></span>
            </li>
          </ul>
        </section>

        <section v-if="report.coverage.unused_operations?.length" class="cc-group">
          <button type="button" class="cc-group-head" :aria-expanded="showUnused" @click="showUnused = !showUnused">
            {{ report.coverage.unused_operations?.length }} operations no request uses<span>{{ showUnused ? 'Hide' : 'Show' }}</span>
          </button>
          <ul v-if="showUnused" class="cc-findings cc-mono"><li v-for="operation in report.coverage.unused_operations" :key="operation">{{ operation }}</li></ul>
        </section>
      </template>
    </template>

    <template v-else-if="tab === 'runtime' && live">
      <p v-if="!live.available" class="kb-rev-note" data-tone="warn"><CircleAlert aria-hidden="true" />{{ live.notes.join(' ') }}</p>
      <template v-else>
        <div class="cc-summary">
          <span class="kb-stat"><b>{{ live.coverage.operations_observed }}/{{ live.coverage.operations_total }}</b>operations seen in traffic</span>
          <span class="kb-stat"><b>{{ live.coverage.requests_total }}</b>requests recorded</span>
          <span class="cc-against">against {{ versionLabel(live.version) }}</span>
        </div>
        <p class="kb-note">Compares route and status code only. Runtime telemetry records no response bodies, so response fields are not checked.</p>
        <p v-for="note in live.notes" :key="note" class="kb-note">{{ note }}</p>
        <p v-if="!live.findings.length && live.coverage.requests_total" class="kb-rev-note" data-tone="ok"><CircleCheck aria-hidden="true" />Every recorded API request used a documented route and status code.</p>
        <section v-for="group in liveGroups" :key="group.severity" class="cc-group">
          <p class="cc-group-head" :data-tone="group.severity"><component :is="icons[group.severity]" aria-hidden="true" />{{ group.findings.length }} {{ severityLabel(group.severity, group.findings.length) }}</p>
          <ul class="cc-findings"><li v-for="finding in group.findings" :key="finding.finding_id"><span>{{ finding.message }}</span></li></ul>
        </section>
        <section v-if="live.coverage.unobserved_operations?.length" class="cc-group">
          <button type="button" class="cc-group-head" :aria-expanded="showUnused" @click="showUnused = !showUnused">
            {{ live.coverage.unobserved_operations.length }} operations with no recorded request<span>{{ showUnused ? 'Hide' : 'Show' }}</span>
          </button>
          <ul v-if="showUnused" class="cc-findings cc-mono"><li v-for="operation in live.coverage.unobserved_operations" :key="operation">{{ operation }}</li></ul>
        </section>
      </template>
    </template>

    <template v-else-if="tab === 'impact' && impact">
      <div v-if="versions.length > 1" class="kb-row3">
        <label class="kb-field">From version
          <select v-model="fromVersion" class="kb-input" @change="loadImpact"><option v-for="version in versions" :key="version.version_id" :value="version.version_id">{{ versionLabel(version) }}</option></select>
        </label>
        <label class="kb-field">To version
          <select v-model="toVersion" class="kb-input" @change="loadImpact"><option v-for="version in versions" :key="version.version_id" :value="version.version_id">{{ versionLabel(version) }}</option></select>
        </label>
      </div>
      <p v-if="!impact.available" class="kb-rev-note" data-tone="warn"><CircleAlert aria-hidden="true" />{{ impact.notes.join(' ') }}</p>
      <template v-else>
        <div class="cc-summary">
          <span class="kb-stat"><b>{{ impact.summary.operations_changed }}</b>operations changed</span>
          <span class="kb-stat"><b>{{ impact.summary.operations_in_use }}</b>used by this project</span>
          <span class="kb-stat"><b>{{ impact.summary.components }}</b>components</span>
          <span class="kb-stat"><b>{{ impact.summary.routes }}</b>routes</span>
          <span v-if="impact.summary.operations_added" class="kb-stat"><b>{{ impact.summary.operations_added }}</b>operations added</span>
        </div>
        <p v-if="!impact.entries.length" class="kb-rev-note" data-tone="ok"><CircleCheck aria-hidden="true" />No operation this comparison covers was removed or changed.</p>
        <ul class="cc-impact">
          <li v-for="entry in impact.entries" :key="entry.operation" :data-used="entry.calls.length > 0">
            <p class="cc-operation"><span class="kb-tag" :data-tone="entry.change === 'removed' ? 'bad' : 'warn'">{{ changeLabel(entry) }}</span><b>{{ entry.operation }}</b></p>
            <p v-if="!entry.calls.length" class="kb-note">Not used by this project.</p>
            <template v-else>
              <p class="cc-line"><b>Requests:</b> <span v-for="call in entry.calls" :key="`${call.file}:${call.line}`" class="cc-where">{{ call.file }}:{{ call.line }}</span></p>
              <p v-if="entry.components.length" class="cc-line"><b>Components:</b> {{ entry.components.map((component) => component.name).join(', ') }}</p>
              <p class="cc-line"><b>Screens:</b> {{ entry.routes.length ? entry.routes.join(', ') : 'no route found for these components' }}</p>
            </template>
          </li>
        </ul>
      </template>
    </template>
  </div>
</template>

<style scoped>
.cc-panel { display: flex; flex: 1 0 100%; flex-direction: column; gap: 0.625rem; min-width: 0; padding-top: 0.625rem; border-top: 1px solid var(--shell-line); }
.cc-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem 1rem; }
.cc-against { margin-left: auto; color: var(--shell-text-3); font-size: 0.75rem; }
.cc-group { display: flex; flex-direction: column; gap: 0.375rem; }
p.cc-group-head { margin: 0; cursor: default; }
.cc-group-head { display: flex; align-items: center; gap: 0.5rem; width: 100%; padding: 0.4375rem 0.625rem; border: 1px solid var(--shell-line); border-radius: 0.5rem; background: transparent; color: var(--shell-text); font-size: 0.8125rem; font-weight: 600; text-align: left; cursor: pointer; }
.cc-group-head svg { width: 0.9375rem; height: 0.9375rem; }
.cc-group-head span { margin-left: auto; color: var(--shell-text-3); font-weight: 400; }
.cc-group-head[data-tone="error"] svg { color: #fb7185; }
.cc-group-head[data-tone="warning"] svg { color: #fcd34d; }
.cc-findings { display: flex; flex-direction: column; gap: 0.25rem; max-height: 22rem; margin: 0; padding: 0 0 0 0.25rem; overflow-y: auto; list-style: none; font-size: 0.8125rem; }
.cc-findings li { display: flex; flex-direction: column; gap: 0.0625rem; padding: 0.3125rem 0.5rem; border-left: 2px solid var(--shell-line-strong); overflow-wrap: anywhere; }
.cc-where { margin-right: 0.625rem; color: var(--shell-text-3); font-family: var(--font-mono); font-size: 0.75rem; overflow-wrap: anywhere; }
.cc-mono { font-family: var(--font-mono); }
.cc-impact { display: flex; flex-direction: column; gap: 0.5rem; margin: 0; padding: 0; list-style: none; font-size: 0.8125rem; }
.cc-impact li { padding: 0.5rem 0.625rem; border: 1px solid var(--shell-line); border-radius: 0.5rem; }
.cc-impact li[data-used="false"] { opacity: 0.7; }
.cc-operation { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; margin: 0; font-family: var(--font-mono); overflow-wrap: anywhere; }
.cc-line { margin: 0.25rem 0 0; overflow-wrap: anywhere; }
/* Inside an attachment row, tags are pushed right by default; here they sit before the operation. */
.cc-operation .kb-tag { margin-left: 0; }
</style>
