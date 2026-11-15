<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { AlertTriangle, CheckCircle2, CircleDashed, Loader2, X, XCircle } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';
import { formatCount, readJson, type DeletionTarget, type PlanStep } from './types';

const props = defineProps<{ target: DeletionTarget | null; title: string }>();
const emit = defineEmits<{ (e: 'close'): void; (e: 'finished'): void }>();

type Plan = { hash: string; steps: PlanStep[]; warnings: string[]; totals: Record<string, number> };
type Job = { job_id: string; state: 'running' | 'succeeded' | 'failed'; steps: PlanStep[]; error: string | null };

const plan = ref<Plan | null>(null);
const job = ref<Job | null>(null);
const loading = ref(false);
const error = ref('');
const typed = ref('');
let poll: ReturnType<typeof setTimeout> | null = null;

const confirmText = computed(() => (props.target?.kind === 'project' ? props.target.project_id : ''));
const canConfirm = computed(() => Boolean(plan.value && !job.value && !loading.value
  && (!confirmText.value || typed.value.trim() === confirmText.value)));
const steps = computed(() => job.value?.steps ?? plan.value?.steps ?? []);
const running = computed(() => job.value?.state === 'running' || loading.value);

const stop = () => { if (poll) clearTimeout(poll); poll = null; };

const loadPlan = async () => {
  if (!props.target) return;
  stop();
  plan.value = null; job.value = null; error.value = ''; typed.value = '';
  loading.value = true;
  try {
    plan.value = await readJson(await makeRequest('/api/data-admin/deletions/plan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target: props.target }),
    }));
  } catch (err: any) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
};

const follow = async (jobId: string) => {
  try {
    job.value = await readJson(await makeRequest(`/api/data-admin/jobs/${encodeURIComponent(jobId)}`));
  } catch (err: any) {
    error.value = err.message;
  }
  if (job.value?.state === 'running') poll = setTimeout(() => follow(jobId), 1500);
  else emit('finished');
};

const execute = async () => {
  if (!canConfirm.value || !plan.value) return;
  loading.value = true;
  error.value = '';
  try {
    const started = await readJson(await makeRequest('/api/data-admin/deletions/execute', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: props.target, hash: plan.value.hash, confirm: typed.value.trim() }),
    }));
    job.value = started;
    await follow(started.job_id);
  } catch (err: any) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
};

const close = () => { if (!running.value) { stop(); emit('close'); } };

watch(() => props.target, (value) => { if (value) loadPlan(); else stop(); }, { immediate: true });
onBeforeUnmount(stop);
</script>

<template>
  <Teleport to=".app-shell">
    <Transition name="app-dialog">
      <div v-if="target" class="dm-dialog-root" role="dialog" aria-modal="true" :aria-label="title">
        <div class="dm-dialog-backdrop" @click="close" />
        <div class="dm-dialog">
          <header class="dm-dialog-head">
            <span class="dm-dialog-icon"><AlertTriangle /></span>
            <div>
              <h3>{{ title }}</h3>
              <p v-if="!job">Dry run: nothing has been deleted yet. Review every step before you confirm.</p>
              <p v-else-if="job.state === 'running'">Deleting. You can keep this open to follow progress.</p>
              <p v-else-if="job.state === 'succeeded'">Finished. The inventory has been refreshed.</p>
              <p v-else>Stopped at a failed step. Fix the cause and run the deletion again to retry.</p>
            </div>
            <button type="button" class="dm-icon-btn" :disabled="running" aria-label="Close" @click="close"><X /></button>
          </header>

          <div v-if="loading && !plan" class="dm-dialog-loading"><Loader2 class="lg-spin" />Planning…</div>

          <ol v-if="steps.length" class="dm-steps">
            <li v-for="(step, index) in steps" :key="index" :data-status="step.status">
              <span class="dm-step-icon">
                <CheckCircle2 v-if="step.status === 'done'" />
                <XCircle v-else-if="step.status === 'failed'" />
                <Loader2 v-else-if="step.status === 'running'" class="lg-spin" />
                <CircleDashed v-else />
              </span>
              <div>
                <p>{{ step.description }}</p>
                <small>{{ step.store }}<template v-if="step.estimate"> · about {{ formatCount(step.estimate) }}</template></small>
                <small v-if="step.error" class="dm-error">{{ step.error }}</small>
              </div>
            </li>
          </ol>

          <ul v-if="plan?.warnings.length && !job" class="dm-warnings">
            <li v-for="warning in plan.warnings" :key="warning">{{ warning }}</li>
          </ul>

          <label v-if="plan && !job && confirmText" class="dm-confirm">
            <span>Type <b>{{ confirmText }}</b> to confirm</span>
            <input v-model="typed" type="text" autocomplete="off" spellcheck="false" />
          </label>

          <p v-if="error" class="dm-error" role="alert">{{ error }}</p>

          <footer class="dm-dialog-foot">
            <button type="button" class="lg-btn" :disabled="running" @click="close">{{ job && !running ? 'Close' : 'Cancel' }}</button>
            <button v-if="!job" type="button" class="lg-btn" data-variant="danger" :disabled="!canConfirm" @click="execute">
              <Loader2 v-if="loading && plan" class="lg-spin" />Delete permanently
            </button>
            <button v-else-if="job.state === 'failed'" type="button" class="lg-btn" :disabled="running" @click="loadPlan">Plan again</button>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
