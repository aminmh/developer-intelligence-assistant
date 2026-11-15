<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { CheckCircle2, FolderGit2, RefreshCw, ShieldCheck, X } from 'lucide-vue-next';
import { phpRuntimeSetupCommand, runtimeSetupReadiness } from '../../utils/runtimeProjects';
import type { RuntimeProjectRecord } from '../../types';

// Browser wizard for local Runtime setup. Every step talks to the desktop helper on this
// computer; nothing changes until Apply, and the helper then asks for approval in a native
// dialog outside the browser before it edits Compose files or recreates services.
const props = defineProps<{
  authorize: () => Promise<string>; knownProjectIds?: string[];
  indexedRevisions?: RuntimeProjectRecord[]; inventoryAvailable?: boolean;
}>();
const emit = defineEmits<{ close: []; apply: [planId: string] }>();

const HELPER = 'http://127.0.0.1:47831';
const SLUG = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const COMMIT = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;

type ComposeProject = { name: string; status: string; directory: string; files: string[] };
type ProjectDetails = { name: string; directory: string; files: string[]; services: string[]; commit: string | null };
type Plan = {
  plan_id: string; expires_in: number; compose_project: string; services: string[]; project_id: string;
  environment: string; commit: string; certificates: 'create' | 'reuse';
  changes: { file: string; added: string[] }[]; approval_items: string[];
};

const step = ref<'project' | 'services' | 'review'>('project');
const projects = ref<ComposeProject[]>([]);
const projectName = ref('');
const details = ref<ProjectDetails | null>(null);
const services = ref<string[]>([]);
const projectId = ref('');
const environment = ref('development');
const commit = ref('');
const plan = ref<Plan | null>(null);
const loading = ref(false);
const error = ref('');

const helper = async <T,>(path: string, body: Record<string, unknown> = {}): Promise<T> => {
  const token = await props.authorize();
  let response: Response;
  try {
    response = await fetch(`${HELPER}${path}`, {
      method: 'POST', cache: 'no-store',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error('The desktop Runtime helper is not reachable. Start Developer Assistant with just up (or just up-dev) on this computer, then try again.');
  }
  const data = await response.json().catch(() => ({}));
  if (response.status === 404) throw new Error('The Runtime helper on this computer is out of date. Run just up to restart it, then reload this page.');
  if (!response.ok) throw new Error(data.detail || 'The Runtime helper could not complete this step.');
  return data as T;
};
const guard = async (work: () => Promise<void>) => {
  loading.value = true;
  error.value = '';
  try { await work(); } catch (cause: any) { error.value = cause?.message || 'Runtime setup failed.'; } finally { loading.value = false; }
};

const loadProjects = () => guard(async () => {
  projects.value = (await helper<{ projects: ComposeProject[] }>('/compose-projects')).projects;
});
const chooseProject = (name: string) => guard(async () => {
  projectName.value = name;
  details.value = await helper<ProjectDetails>('/compose-project', { project_name: name });
  services.value = [];
  projectId.value = suggestedProjectId(name);
  commit.value = details.value.commit || '';
  plan.value = null;
  step.value = 'services';
});
// Containers only match indexed revisions under the same project ID, so prefer an existing one:
// an exact match, else the longest known ID the Compose name starts with (pizza-builder → pizza).
const suggestedProjectId = (composeName: string) => {
  const known = props.knownProjectIds || [];
  const lower = composeName.toLowerCase();
  const exact = known.find((id) => id.toLowerCase() === lower);
  if (exact) return exact;
  const prefix = known.filter((id) => lower.startsWith(id.toLowerCase()) && '-_.'.includes(lower.charAt(id.length)))
    .sort((a, b) => b.length - a.length)[0];
  return prefix || (SLUG.test(composeName) ? composeName : '');
};
const isKnownProject = computed(() => (props.knownProjectIds || []).includes(projectId.value));
const readiness = computed(() => runtimeSetupReadiness(projectId.value, commit.value,
  props.indexedRevisions || [], props.inventoryAvailable === true));
const tracingCommand = computed(() => phpRuntimeSetupCommand(details.value?.directory || '/path/to/project', projectId.value));
const inputProblem = computed(() => {
  if (!services.value.length) return 'Choose at least one service.';
  if (!SLUG.test(projectId.value)) return 'Project ID: 1–128 letters, numbers, dots, dashes or underscores.';
  if (!SLUG.test(environment.value)) return 'Environment: 1–128 letters, numbers, dots, dashes or underscores.';
  if (!COMMIT.test(commit.value)) return 'Commit: the full lowercase 40- or 64-character Git SHA.';
  return '';
});
const review = () => guard(async () => {
  plan.value = await helper<Plan>('/setup/plan', {
    project_name: projectName.value, services: services.value,
    project_id: projectId.value, environment: environment.value, commit: commit.value,
  });
  step.value = 'review';
});
const apply = () => { if (plan.value) emit('apply', plan.value.plan_id); };
const toggleService = (service: string) => {
  services.value = services.value.includes(service) ? services.value.filter((item) => item !== service) : [...services.value, service];
};

onMounted(loadProjects);
</script>

<template>
  <section class="rounded-2xl border border-emerald-500/25 bg-emerald-500/[0.04] p-5" aria-label="Set up Runtime">
    <header class="flex items-start justify-between gap-3">
      <div>
        <h4 class="text-sm font-semibold text-white">Set up Runtime for a local Compose project</h4>
        <p class="mt-1 text-[11px] leading-5 text-slate-400">The Developer Assistant helper on this computer reads your Compose projects. Nothing changes until you apply, and then a dialog on this computer asks you to allow the changes.</p>
      </div>
      <button type="button" class="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800" aria-label="Close setup" @click="emit('close')"><X class="h-4 w-4" /></button>
    </header>

    <ol class="mt-4 flex flex-wrap gap-2 text-[11px]" aria-label="Setup steps">
      <li v-for="(label, key) in { project: '1. Project', services: '2. Services', review: '3. Review' }" :key="key"
          class="rounded-full border px-2.5 py-1" :class="step === key ? 'border-emerald-400/50 text-emerald-200' : 'border-slate-700 text-slate-500'" :aria-current="step === key ? 'step' : undefined">{{ label }}</li>
    </ol>

    <p v-if="error" role="alert" class="mt-3 rounded-lg border border-rose-500/25 bg-rose-950/20 p-3 text-xs text-rose-300">{{ error }}</p>

    <aside v-if="details && step !== 'project'" class="mt-4 rounded-lg border border-cyan-500/25 bg-cyan-500/5 p-3 text-xs leading-5 text-slate-300" aria-label="Request tracing readiness">
      <h5 class="font-semibold text-white">{{ readiness.status === 'ready' ? 'Matching source revision is indexed' : readiness.status === 'unknown' ? 'Source indexing could not be checked' : readiness.status === 'mismatch' ? 'This Git commit is not indexed' : 'Source indexing is needed for request tracing' }}</h5>
      <p v-if="readiness.status === 'ready'">Source revision <code>{{ readiness.revisionId }}</code> matches this commit. PHP must still export traces with this project ID and revision ID.</p>
      <p v-else-if="readiness.status === 'mismatch'">This project has indexed source, but no ready revision matches the selected Git commit. A ZIP upload alone does not verify a match to the running commit.</p>
      <p v-else-if="readiness.status === 'missing'">No ready indexed revision was found for this project ID.</p>
      <p v-else>Refresh Runtime to check the indexed source before configuring tracing.</p>
      <p class="mt-1">You can continue to monitor containers now. This wizard adds container labels; it does not install PHP tracing or index source.</p>
      <p class="mt-2">For PHP request tracing, run this from the Developer Assistant folder:</p>
      <pre class="mt-1 overflow-x-auto rounded bg-black/30 p-2 text-xs"><code>{{ tracingCommand }}</code></pre>
      <p class="mt-1">The command indexes committed source, configures the PHP adapter and revision ID, and restarts the PHP service after approval. Then open a page in your app, refresh Runtime and select the matching revision.</p>
    </aside>

    <!-- 1. Compose project -->
    <div v-if="step === 'project'" class="mt-4">
      <div class="flex items-center justify-between gap-2">
        <p class="text-xs text-slate-300">Choose the Docker Compose project whose services should report.</p>
        <button type="button" class="inline-flex items-center gap-1 rounded-lg border border-slate-700 px-2 py-1 text-[11px] text-slate-300 hover:bg-slate-800 disabled:opacity-50" :disabled="loading" @click="loadProjects"><RefreshCw class="h-3 w-3" :class="loading ? 'animate-spin' : ''" />Reload</button>
      </div>
      <p v-if="loading && !projects.length" class="mt-3 text-xs text-slate-400">Looking for Compose projects on this computer…</p>
      <p v-else-if="!loading && !projects.length && !error" class="mt-3 rounded-lg border border-dashed border-slate-700 p-3 text-xs text-slate-400">Docker Compose reported no projects. Start your project with <code>docker compose up</code>, then reload.</p>
      <ul class="mt-3 space-y-1.5">
        <li v-for="item in projects" :key="item.name">
          <button type="button" class="flex w-full items-start gap-3 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-left hover:border-emerald-400/40 disabled:opacity-50" :disabled="loading" @click="chooseProject(item.name)">
            <FolderGit2 class="mt-0.5 h-4 w-4 shrink-0 text-emerald-300" />
            <span class="min-w-0 flex-1">
              <span class="block text-xs font-medium text-slate-100">{{ item.name }} <span class="font-normal text-slate-500">· {{ item.status }}</span></span>
              <span class="block break-all font-mono text-[10px] text-slate-500">{{ item.files.join(', ') }}</span>
            </span>
          </button>
        </li>
      </ul>
    </div>

    <!-- 2. Services and identity -->
    <form v-else-if="step === 'services' && details" class="mt-4 space-y-4" @submit.prevent="review">
      <p class="text-xs text-slate-300">Compose project <strong class="text-white">{{ details.name }}</strong> <span class="break-all font-mono text-[10px] text-slate-500">{{ details.directory }}</span></p>
      <fieldset>
        <legend class="text-xs font-medium text-slate-200">Services to report</legend>
        <p class="text-[11px] text-slate-500">Each chosen service gets telemetry labels and is recreated. Others are not touched.</p>
        <div class="mt-2 flex flex-wrap gap-2">
          <label v-for="service in details.services" :key="service" class="inline-flex cursor-pointer items-center gap-2 rounded-lg border px-2.5 py-1.5 text-xs" :class="services.includes(service) ? 'border-emerald-400/50 bg-emerald-500/10 text-emerald-100' : 'border-slate-700 text-slate-300'">
            <input type="checkbox" class="h-3.5 w-3.5 accent-emerald-400" :checked="services.includes(service)" @change="toggleService(service)" />{{ service }}
          </label>
        </div>
      </fieldset>
      <div class="grid gap-3 sm:grid-cols-3">
        <label class="text-[11px] text-slate-400">Runtime project ID
          <input v-model.trim="projectId" required list="runtime-known-project-ids" class="mt-1 h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-2.5 text-xs text-slate-100" placeholder="billing" />
          <datalist id="runtime-known-project-ids"><option v-for="id in knownProjectIds || []" :key="id" :value="id" /></datalist>
          <span class="mt-1 block text-[10px] leading-4" :class="isKnownProject ? 'text-emerald-300' : 'text-amber-300'">{{ isKnownProject ? 'Known project ID. Request tracing also needs the matching indexed commit.' : 'New project ID. Use this same ID when indexing and configuring PHP tracing.' }}</span>
        </label>
        <label class="text-[11px] text-slate-400">Environment
          <input v-model.trim="environment" required class="mt-1 h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-2.5 text-xs text-slate-100" />
        </label>
        <label class="text-[11px] text-slate-400">Git commit {{ details.commit ? '(detected)' : '' }}
          <input v-model.trim="commit" required class="mt-1 h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-2.5 font-mono text-[11px] text-slate-100" placeholder="full 40-character SHA" />
        </label>
      </div>
      <p v-if="inputProblem" class="text-[11px] text-amber-300">{{ inputProblem }}</p>
      <div class="flex justify-between gap-2">
        <button type="button" class="rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800" @click="step = 'project'">Back</button>
        <button type="submit" class="rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-semibold text-slate-950 disabled:opacity-50" :disabled="loading || !!inputProblem">{{ loading ? 'Preparing preview…' : 'Review changes' }}</button>
      </div>
    </form>

    <!-- 3. Review -->
    <div v-else-if="step === 'review' && plan" class="mt-4 space-y-3">
      <p class="text-xs text-slate-300"><strong class="text-white">{{ plan.project_id }}</strong> · {{ plan.environment }} · <code>{{ plan.commit.slice(0, 12) }}</code> — services {{ plan.services.join(', ') }} in {{ plan.compose_project }}.</p>
      <div>
        <p class="text-xs font-medium text-slate-200">What will happen</p>
        <ul class="mt-1.5 space-y-1 text-[11px] text-slate-400">
          <li v-for="item in plan.approval_items" :key="item" class="flex gap-2"><CheckCircle2 class="mt-0.5 h-3 w-3 shrink-0 text-emerald-400" /><span class="break-all">{{ item }}</span></li>
        </ul>
      </div>
      <div v-for="change in plan.changes" :key="change.file">
        <p class="break-all font-mono text-[10px] text-slate-400">{{ change.file }} — lines added</p>
        <pre class="mt-1 max-h-56 overflow-auto rounded-lg border border-slate-800 bg-black/30 p-2 text-[10px] leading-4 text-emerald-200"><code>{{ change.added.map((line) => `+ ${line}`).join('\n') }}</code></pre>
      </div>
      <p class="flex gap-2 rounded-lg border border-cyan-500/20 bg-cyan-500/5 p-2.5 text-[11px] text-cyan-100"><ShieldCheck class="mt-0.5 h-3.5 w-3.5 shrink-0" />After you click Apply, a dialog on this computer lists these changes. Nothing is changed unless you click Allow there. If a Compose file changes before then, setup stops and asks you to review again. This preview expires in {{ Math.round(plan.expires_in / 60) }} minutes.</p>
      <div class="flex justify-between gap-2">
        <button type="button" class="rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800" @click="step = 'services'">Back</button>
        <button type="button" class="rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-semibold text-slate-950" @click="apply">Apply</button>
      </div>
    </div>
  </section>
</template>
