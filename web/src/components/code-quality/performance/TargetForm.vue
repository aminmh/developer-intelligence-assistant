<script setup lang="ts">
import { computed } from 'vue';
import { Plus, Save, Trash2, X } from 'lucide-vue-next';
import { LOGIN_PRESETS, isLocalOrigin, peakUsers, runSeconds, type Target } from './performance';

// Target settings: where to send load, how to authenticate, and the workload model.
const target = defineModel<Target>({ required: true });
const props = defineProps<{ credentialSaved: boolean; saving: boolean; dirty: boolean; selectedRouteCount: number; canOperate: boolean }>();
const emit = defineEmits<{ save: []; remove: []; saveToken: [token: string] }>();
const tokenInput = defineModel<string>('token', { default: '' });
const loginUsername = defineModel<string>('username', { default: '' });
const loginPassword = defineModel<string>('password', { default: '' });

const field = 'mt-1 block min-h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-2.5 text-sm text-slate-100 disabled:opacity-50';
const httpsWarning = computed(() => target.value.auth_mode !== 'public' && target.value.origin.startsWith('http://') && !isLocalOrigin(target.value.origin));
const totalUsers = computed(() => peakUsers(target.value) * Math.max(1, props.selectedRouteCount));
const applyPreset = (id: string) => {
  const preset = LOGIN_PRESETS.find((item) => item.id === id);
  if (preset) target.value.login = { ...target.value.login, ...preset.login } as Target['login'];
};
const addStage = () => { (target.value.stages ||= []).push({ duration_seconds: 10, target: peakUsers(target.value) || 1 }); };
const setBudget = (key: 'p95_ms' | 'error_rate', value: string) => {
  const next = { ...target.value.thresholds };
  if (value === '') delete next[key]; else next[key] = key === 'error_rate' ? Number(value) / 100 : Number(value);
  target.value.thresholds = next;
};
</script>

<template>
  <fieldset class="space-y-4" :disabled="!canOperate">
    <div class="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
      <label class="text-sm text-slate-300">Name<input v-model="target.name" maxlength="80" :class="field" placeholder="Local Docker" /></label>
      <label class="text-sm text-slate-300">Target origin
        <input v-model="target.origin" type="url" required :class="field" placeholder="http://host.docker.internal:8080" />
      </label>
    </div>
    <p class="-mt-2 text-xs text-slate-500">For an app running in local Docker use <code>host.docker.internal</code> with the port it publishes. A name lets one origin have several profiles (for example public and logged in).</p>

    <fieldset>
      <legend class="text-sm font-medium text-slate-300">Authentication</legend>
      <div class="mt-2 grid gap-2 sm:grid-cols-3">
        <label v-for="mode in ([['public', 'Public'], ['bearer', 'Bearer token'], ['http_login', 'Login form']] as const)" :key="mode[0]" class="flex min-h-9 items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/60 px-3 text-sm text-slate-300"><input v-model="target.auth_mode" type="radio" :value="mode[0]" />{{ mode[1] }}</label>
      </div>
      <p v-if="httpsWarning" role="alert" class="mt-2 text-xs text-amber-200">Use an https:// origin: tokens and passwords are refused over plain HTTP to a non-local host.</p>
    </fieldset>

    <div v-if="target.auth_mode === 'bearer'" class="space-y-2 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <p class="text-sm text-slate-300">{{ credentialSaved ? 'A token for this target is saved in the OS credential store.' : target.target_id ? 'Save the token once; it stays in the OS credential store.' : 'Save the target first, then its token.' }}</p>
      <div class="flex gap-2">
        <input v-model="tokenInput" type="password" autocomplete="new-password" :disabled="!target.target_id" :class="field" :placeholder="credentialSaved ? 'Replace the token' : 'Bearer token'" aria-label="Bearer token" />
        <button type="button" :disabled="saving || !tokenInput || !target.target_id" class="mt-1 inline-flex min-h-9 shrink-0 items-center gap-1.5 rounded-lg border border-slate-700 px-3 text-sm disabled:opacity-50" @click="emit('saveToken', tokenInput)"><Save :size="14" />Save token</button>
      </div>
    </div>

    <div v-if="target.auth_mode === 'http_login'" class="space-y-3 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <div class="flex flex-wrap items-center gap-2 text-xs text-slate-400">Preset:
        <button v-for="preset in LOGIN_PRESETS" :key="preset.id" type="button" class="min-h-7 rounded-md border border-slate-700 px-2 text-slate-300 hover:bg-slate-800" @click="applyPreset(preset.id)">{{ preset.name }}</button>
      </div>
      <div class="grid gap-3 sm:grid-cols-2">
        <label class="text-xs text-slate-400">Login path<input v-model="target.login.login_path" :class="field" /></label>
        <label class="text-xs text-slate-400">Logged-in check route (recommended)<input v-model="target.login.check_path" :class="field" placeholder="/dashboard" /></label>
        <label class="text-xs text-slate-400">Username field<input v-model="target.login.username_field" :class="field" /></label>
        <label class="text-xs text-slate-400">Password field<input v-model="target.login.password_field" :class="field" /></label>
        <label class="text-xs text-slate-400">CSRF protection
          <select v-model="target.login.csrf_mode" :class="field">
            <option value="header">Cookie → header (Laravel XSRF-TOKEN)</option>
            <option value="field">Cookie → form field</option>
            <option value="none">None</option>
          </select>
        </label>
        <label v-if="target.login.csrf_mode !== 'none'" class="text-xs text-slate-400">CSRF bootstrap path<input v-model="target.login.csrf_path" :class="field" placeholder="/login" /></label>
        <label v-if="target.login.csrf_mode !== 'none'" class="text-xs text-slate-400">CSRF cookie<input v-model="target.login.csrf_cookie_name" :class="field" /></label>
        <label v-if="target.login.csrf_mode === 'header'" class="text-xs text-slate-400">CSRF header<input v-model="target.login.csrf_header_name" :class="field" /></label>
        <label v-if="target.login.csrf_mode === 'field'" class="text-xs text-slate-400">CSRF form field<input v-model="target.login.csrf_field_name" :class="field" /></label>
        <label class="text-xs text-slate-400">Run username<input v-model="loginUsername" autocomplete="username" :class="field" /></label>
        <label class="text-xs text-slate-400">Run password<input v-model="loginPassword" type="password" autocomplete="current-password" :class="field" /></label>
      </div>
      <p class="text-xs leading-relaxed text-slate-500">Each virtual user logs in once; login time is excluded from results. A login counts only when it does not redirect back to the login page, and the check route (if set) returns 2xx. Credentials are used for this run only and never saved.</p>
    </div>

    <fieldset class="space-y-3 rounded-lg border border-slate-800 p-3">
      <legend class="px-1 text-sm font-medium text-slate-300">Workload (each route runs as its own scenario)</legend>
      <div class="grid gap-2 sm:grid-cols-3">
        <label v-for="mode in ([['constant_vus', 'Constant users'], ['ramping_vus', 'Ramp users'], ['constant_arrival_rate', 'Requests per second']] as const)" :key="mode[0]" class="flex min-h-9 items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/60 px-3 text-sm text-slate-300"><input v-model="target.executor" type="radio" :value="mode[0]" />{{ mode[1] }}</label>
      </div>
      <div v-if="target.executor === 'constant_vus'" class="grid gap-3 sm:grid-cols-2">
        <label class="text-xs text-slate-400">Virtual users per route<input v-model.number="target.virtual_users" type="number" min="1" max="50" :class="field" /></label>
        <label class="text-xs text-slate-400">Duration (seconds)<input v-model.number="target.duration_seconds" type="number" min="5" max="300" :class="field" /></label>
      </div>
      <div v-else-if="target.executor === 'constant_arrival_rate'" class="grid gap-3 sm:grid-cols-3">
        <label class="text-xs text-slate-400">Requests per second per route<input v-model.number="target.rate_per_second" type="number" min="1" max="500" :class="field" /></label>
        <label class="text-xs text-slate-400">Max virtual users per route<input v-model.number="target.max_virtual_users" type="number" min="1" max="50" :class="field" /></label>
        <label class="text-xs text-slate-400">Duration (seconds)<input v-model.number="target.duration_seconds" type="number" min="5" max="300" :class="field" /></label>
      </div>
      <div v-else class="space-y-2">
        <div v-for="(stage, index) in target.stages" :key="index" class="flex items-end gap-2">
          <label class="flex-1 text-xs text-slate-400">Stage {{ index + 1 }} duration (s)<input v-model.number="stage.duration_seconds" type="number" min="1" max="300" :class="field" /></label>
          <label class="flex-1 text-xs text-slate-400">Target users<input v-model.number="stage.target" type="number" min="0" max="50" :class="field" /></label>
          <button type="button" class="mb-0.5 rounded-lg border border-slate-700 p-2 text-slate-400 disabled:opacity-40" :disabled="(target.stages || []).length < 2" :aria-label="`Remove stage ${index + 1}`" @click="target.stages!.splice(index, 1)"><X :size="14" /></button>
        </div>
        <button type="button" class="inline-flex min-h-8 items-center gap-1 rounded-lg border border-slate-700 px-2.5 text-xs text-slate-300 disabled:opacity-40" :disabled="(target.stages || []).length >= 10" @click="addStage"><Plus :size="12" />Add stage</button>
      </div>
      <div class="grid gap-3 sm:grid-cols-4">
        <label class="text-xs text-slate-400">Think time (ms)<input v-model.number="target.think_time_ms" type="number" min="0" max="2000" :class="field" /></label>
        <label class="text-xs text-slate-400">Warm-up (s, not measured)<input v-model.number="target.warmup_seconds" type="number" min="0" max="60" :class="field" /></label>
        <label class="text-xs text-slate-400">p95 budget (ms)<input :value="target.thresholds.p95_ms ?? ''" type="number" min="1" :class="field" placeholder="none" @input="setBudget('p95_ms', ($event.target as HTMLInputElement).value)" /></label>
        <label class="text-xs text-slate-400">Error budget (%)<input :value="target.thresholds.error_rate != null ? target.thresholds.error_rate * 100 : ''" type="number" min="0" max="100" step="0.1" :class="field" placeholder="none" @input="setBudget('error_rate', ($event.target as HTMLInputElement).value)" /></label>
      </div>
      <p class="text-xs text-slate-500">Up to {{ totalUsers }} virtual users in total for {{ runSeconds(target) }}s{{ target.warmup_seconds ? ` after a ${target.warmup_seconds}s warm-up` : '' }}. The limit is 100 users across all routes.</p>
    </fieldset>

    <div class="flex flex-wrap items-center gap-2">
      <button type="button" :disabled="saving" class="inline-flex min-h-10 items-center gap-2 rounded-lg border border-slate-700 px-4 text-sm font-medium disabled:opacity-50" @click="emit('save')"><Save :size="15" />{{ saving ? 'Saving…' : target.target_id ? 'Save target' : 'Create target' }}</button>
      <button v-if="target.target_id" type="button" :disabled="saving" class="inline-flex min-h-10 items-center gap-2 rounded-lg border border-rose-500/30 px-3 text-sm text-rose-200 disabled:opacity-50" @click="emit('remove')"><Trash2 :size="14" />Remove</button>
      <span v-if="dirty" class="rounded-full bg-amber-500/10 px-2 py-0.5 text-xs text-amber-200">Unsaved changes: saved before the next run</span>
    </div>
  </fieldset>
</template>
