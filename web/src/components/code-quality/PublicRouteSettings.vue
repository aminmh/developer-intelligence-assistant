<script setup lang="ts">
// Routes a project declares intentionally public, so the route-authorization check reports them as
// public instead of "no authorization" (api: code_quality/public_routes.py).
import { computed, ref, watch } from 'vue';
import { LoaderCircle, Save, X } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';

const props = defineProps<{ projectId?: string | null; disabled?: boolean }>();

const patterns = ref<string[]>([]);
const saved = ref<string[]>([]);
const input = ref('');
const loading = ref(false);
const saving = ref(false);
const error = ref('');
const message = ref('');
let request = 0;

const changed = computed(() => JSON.stringify([...patterns.value].sort()) !== JSON.stringify([...saved.value].sort()));
const busy = computed(() => loading.value || saving.value || !!props.disabled || !props.projectId);
const url = (projectId: string) => `/api/code-quality/projects/${encodeURIComponent(projectId)}/public-routes`;

const readError = async (response: Response, fallback: string) => {
  const body = await response.json().catch(() => ({}));
  return body.detail || body.error || fallback;
};

const load = async (projectId?: string | null) => {
  const requestId = ++request;
  error.value = '';
  message.value = '';
  patterns.value = [];
  saved.value = [];
  if (!projectId) return;
  loading.value = true;
  try {
    const response = await makeRequest(url(projectId));
    if (!response.ok) throw new Error(await readError(response, 'Public routes could not be loaded.'));
    const body = await response.json();
    if (requestId !== request) return;
    saved.value = body.patterns || [];
    patterns.value = [...saved.value];
  } catch (cause: any) {
    if (requestId === request) error.value = cause?.message || 'Public routes could not be loaded.';
  } finally {
    if (requestId === request) loading.value = false;
  }
};

const add = () => {
  const pattern = input.value.trim().replace(/\s+/g, ' ');
  if (!pattern || patterns.value.includes(pattern)) return;
  patterns.value = [...patterns.value, pattern];
  input.value = '';
};

const save = async () => {
  if (!props.projectId || saving.value) return;
  saving.value = true;
  error.value = '';
  message.value = '';
  try {
    const response = await makeRequest(url(props.projectId), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ patterns: patterns.value }),
    });
    if (!response.ok) throw new Error(await readError(response, 'Public routes could not be saved.'));
    const body = await response.json();
    saved.value = body.patterns || [];
    patterns.value = [...saved.value];
    message.value = 'Public routes saved for this project.';
  } catch (cause: any) {
    error.value = cause?.message || 'Public routes could not be saved.';
  } finally {
    saving.value = false;
  }
};

watch(() => props.projectId, (projectId) => load(projectId), { immediate: true });
</script>

<template>
  <div>
    <h3 class="cq-h4">Intentionally public routes</h3>
    <p class="cq-hint">
      Routes that need no login or role by design, such as sign-in, one-time codes or a guest cart. The route-authorization
      check reports a matching route as public instead of “no authorization”, and names the pattern. Routes that are
      protected stay protected.
    </p>
    <div v-if="loading" class="cq-hint cq-inline"><LoaderCircle class="cq-spin" aria-hidden="true" />Loading public routes…</div>
    <form class="cq-addrow" @submit.prevent="add">
      <label for="quality-public-route" class="sr-only">Add a public route pattern</label>
      <input id="quality-public-route" v-model="input" class="cq-input" :disabled="busy" type="text" maxlength="300"
             placeholder="Add a route, e.g. POST /api/v1/auth/*" />
      <button type="submit" class="cq-btn cq-btn-sm" :disabled="!input.trim() || busy">Add</button>
    </form>
    <ul v-if="patterns.length" class="cq-pats" :aria-label="`Public route patterns (${patterns.length})`">
      <li v-for="pattern in patterns" :key="pattern" class="cq-pat">
        <span>{{ pattern }}</span>
        <button type="button" :disabled="busy" :aria-label="`Remove public route ${pattern}`"
                @click="patterns = patterns.filter((item) => item !== pattern)"><X aria-hidden="true" /></button>
      </li>
    </ul>
    <p class="cq-hint">
      Write a method and a path as shown in the report: <code>POST /api/v1/auth/*</code>. Use <code>*</code> as the method
      for every method; in the path, <code>*</code> matches anything, including <code>/</code>.
    </p>
    <p v-if="error" class="cq-hint" data-tone="bad" role="alert">{{ error }}</p>
    <p v-else-if="message" class="cq-hint" role="status">{{ message }}</p>
    <div class="cq-inline">
      <button type="button" class="cq-btn cq-btn-sm" :disabled="busy || !changed" @click="save">
        <LoaderCircle v-if="saving" class="cq-spin" aria-hidden="true" /><Save v-else aria-hidden="true" />{{ saving ? 'Saving…' : 'Save public routes' }}
      </button>
    </div>
  </div>
</template>
