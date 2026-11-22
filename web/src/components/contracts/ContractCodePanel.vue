<script setup lang="ts">
/**
 * Generate typed client code or mocks from one contract version and show the files.
 * Plan: .archcore/frontend-assist/frontend-developer-assistance.plan.md (Phase 5)
 */
import { computed, ref, watch } from 'vue';
import { CircleCheck, Copy, Download, LoaderCircle, Wand2 } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';
import { toastMessage } from '../../composables/useToasts';
import { CODE_TARGETS, lineCount, type CodeTarget, type GeneratedCode } from '../../utils/contractCode';

const props = defineProps<{ sourceId: string; versionId: string; defaultTarget?: CodeTarget }>();
const target = ref<CodeTarget>(props.defaultTarget || 'models');
const search = ref('');
const baseUrl = ref('');
const result = ref<GeneratedCode | null>(null);
const activePath = ref('');
const loading = ref(false);
const error = toastMessage('error');
const copied = ref('');

const hint = computed(() => CODE_TARGETS.find((item) => item.id === target.value)?.hint || '');
const activeFile = computed(() => result.value?.files.find((file) => file.path === activePath.value) || result.value?.files[0] || null);

let sequence = 0;
const generate = async () => {
  const current = ++sequence;
  loading.value = true; error.value = ''; copied.value = '';
  try {
    const body: Record<string, string> = { target: target.value };
    if (search.value.trim()) body.search = search.value.trim();
    if (baseUrl.value.trim()) body.base_url = baseUrl.value.trim();
    const response = await makeRequest(`/api/contracts/sources/${props.sourceId}/versions/${props.versionId}/generate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await response.json().catch(() => ({}));
    if (current !== sequence) return;
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Code could not be generated.');
    result.value = data;
    // Open the file that is specific to the target; models.ts is always first.
    activePath.value = data.files[data.files.length > 1 ? 1 : 0]?.path || '';
  } catch (cause: any) {
    if (current === sequence) { error.value = cause?.message || 'Code could not be generated.'; result.value = null; }
  } finally {
    if (current === sequence) loading.value = false;
  }
};
// A different version or target makes the shown files stale.
watch(() => [props.versionId, target.value] as const, () => { result.value = null; error.value = ''; });

const copy = async () => {
  if (!activeFile.value) return;
  try { await navigator.clipboard.writeText(activeFile.value.content); copied.value = activeFile.value.path; }
  catch { error.value = 'The browser did not allow copying. Select the code and copy it by hand.'; }
};
const download = () => {
  if (!activeFile.value) return;
  const url = URL.createObjectURL(new Blob([activeFile.value.content], { type: 'text/plain;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url; link.download = activeFile.value.path;
  link.click();
  URL.revokeObjectURL(url);
};
</script>

<template>
  <div class="gc-panel">
    <div class="kb-row3">
      <label class="kb-field">Generate
        <select v-model="target" class="kb-input">
          <option v-for="item in CODE_TARGETS" :key="item.id" :value="item.id">{{ item.label }}</option>
        </select>
      </label>
      <label class="kb-field">Only operations matching
        <input v-model="search" class="kb-input" maxlength="100" autocomplete="off" placeholder="All (or a path, tag, or operation ID)" @keydown.enter="generate" />
      </label>
      <label v-if="target !== 'models'" class="kb-field">Base address
        <input v-model="baseUrl" class="kb-input" maxlength="300" autocomplete="off" placeholder="From the contract" @keydown.enter="generate" />
      </label>
    </div>
    <p class="kb-note">{{ hint }}</p>
    <div class="kb-btns">
      <button type="button" class="kb-btn" data-variant="primary" :disabled="loading" @click="generate">
        <LoaderCircle v-if="loading" class="kb-spin" aria-hidden="true" /><Wand2 v-else aria-hidden="true" />Generate
      </button>
    </div>

    <template v-if="result && activeFile">
      <p v-for="note in result.notes" :key="note" class="kb-note">{{ note }}</p>
      <div class="gc-bar">
        <div class="kb-tabs" role="tablist" aria-label="Generated files">
          <button v-for="file in result.files" :key="file.path" type="button" role="tab" :aria-selected="file.path === activeFile.path" @click="activePath = file.path; copied = ''">{{ file.path }}</button>
        </div>
        <span class="gc-meta">{{ result.operations }} operations · {{ lineCount(activeFile.content) }} lines</span>
        <button type="button" class="kb-btn" @click="copy"><CircleCheck v-if="copied === activeFile.path" aria-hidden="true" /><Copy v-else aria-hidden="true" />{{ copied === activeFile.path ? 'Copied' : 'Copy' }}</button>
        <button type="button" class="kb-btn" @click="download"><Download aria-hidden="true" />Download</button>
      </div>
      <pre class="gc-code" tabindex="0" :aria-label="`Contents of ${activeFile.path}`"><code>{{ activeFile.content }}</code></pre>
    </template>
  </div>
</template>

<style scoped>
.gc-panel { display: flex; flex: 1 0 100%; flex-direction: column; grid-column: 1 / -1; gap: 0.5rem; min-width: 0; padding-top: 0.625rem; border-top: 1px solid var(--shell-line); }
.gc-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; }
.gc-meta { margin-left: auto; color: var(--shell-text-3); font-size: 0.75rem; }
.gc-code { max-height: 28rem; margin: 0; padding: 0.75rem; overflow: auto; border: 1px solid var(--shell-line); border-radius: 0.625rem; background: var(--kb-sunken); color: var(--shell-text); font-family: var(--font-mono); font-size: 0.75rem; line-height: 1.55; tab-size: 2; white-space: pre; }
</style>
