<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { makeRequest } from '../ui/makeRequest';
import type { RuntimeHostMetricsResponse, RuntimeStorageTarget } from '../types';

const POLL_INTERVAL_MS = 5_000;
const metrics = ref<RuntimeHostMetricsResponse | null>(null);
const remoteMode = ref(false);
const firstRequestPending = ref(true);

let alive = false;
let pollTimer: number | undefined;
let activeRequest: AbortController | undefined;
let requestInFlight = false;

const sampledAtLabel = computed(() => {
  const sampledAt = metrics.value?.sampled_at;
  if (!sampledAt) return '';
  const date = new Date(sampledAt);
  return Number.isNaN(date.getTime()) ? sampledAt : date.toLocaleString();
});

function formatBytes(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return `${amount.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function healthLabel(value: string | undefined): string {
  if (value === 'connected' || value === 'available') return 'Available';
  if (value === 'unavailable') return 'Unavailable';
  if (value === 'not-required') return 'Not required';
  return 'Checking';
}

function healthClasses(value: string | undefined): string {
  if (value === 'connected' || value === 'available') return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200';
  if (value === 'unavailable') return 'border-rose-500/30 bg-rose-500/10 text-rose-200';
  return 'border-slate-700 bg-slate-800/50 text-slate-300';
}

function clearPollTimer() {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
}

function applyApiUnavailable(message: string) {
  const previous = metrics.value;
  metrics.value = {
    mode: 'remote',
    available: false,
    stale: Boolean(previous?.metrics),
    sampled_at: previous?.sampled_at ?? null,
    ssh_health: 'unavailable',
    ssh_message: message,
    clickhouse_health: 'unknown',
    clickhouse_message: 'ClickHouse health could not be checked because the Runtime API did not respond.',
    metrics: previous?.metrics ?? null,
  };
}

async function poll() {
  if (!alive || document.visibilityState === 'hidden' || requestInFlight) return;
  requestInFlight = true;
  const request = new AbortController();
  activeRequest = request;

  try {
    if (!remoteMode.value) {
      const modeResponse = await makeRequest('/api/runtime/settings/storage', { signal: request.signal });
      const target = await modeResponse.json().catch(() => null) as RuntimeStorageTarget | null;
      if (!modeResponse.ok || !target) throw new Error('Runtime storage settings are unavailable.');
      if (target.mode !== 'remote') {
        remoteMode.value = false;
        metrics.value = null;
        clearPollTimer();
        return;
      }
      remoteMode.value = true;
    }

    const response = await makeRequest('/api/runtime/host-metrics', { signal: request.signal });
    const data = await response.json().catch(() => null) as RuntimeHostMetricsResponse | null;
    if (!response.ok || !data) throw new Error('Remote host metrics are unavailable.');
    if (data.mode !== 'remote') {
      remoteMode.value = false;
      metrics.value = null;
      clearPollTimer();
      return;
    }
    remoteMode.value = true;
    metrics.value = data;
  } catch {
    if (request.signal.aborted) return;
    if (remoteMode.value) {
      applyApiUnavailable('The Runtime API could not reach the configured host. Check the application connection and retry.');
    }
  } finally {
    if (activeRequest === request) activeRequest = undefined;
    requestInFlight = false;
    firstRequestPending.value = false;
  }
}

function startPolling() {
  if (!alive || document.visibilityState === 'hidden' || pollTimer !== undefined) return;
  void poll();
  pollTimer = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
}

function onVisibilityChange() {
  if (document.visibilityState === 'hidden') {
    clearPollTimer();
    activeRequest?.abort();
    return;
  }
  // Re-read the mode after a hidden interval so a mode change never leaves a stale monitor visible.
  remoteMode.value = false;
  metrics.value = null;
  firstRequestPending.value = true;
  startPolling();
}

onMounted(() => {
  alive = true;
  document.addEventListener('visibilitychange', onVisibilityChange);
  startPolling();
});

onUnmounted(() => {
  alive = false;
  document.removeEventListener('visibilitychange', onVisibilityChange);
  clearPollTimer();
  activeRequest?.abort();
});
</script>

<template>
  <Transition name="app-panel">
    <section
      v-if="remoteMode"
      aria-labelledby="remote-host-monitor-title"
      class="rounded-2xl border border-cyan-500/20 bg-slate-900/50 p-5"
    >
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h4 id="remote-host-monitor-title" class="flex items-center gap-2 text-sm font-semibold text-white">
            <span aria-hidden="true" class="h-2 w-2 rounded-full" :class="metrics?.available && !metrics.stale && metrics.clickhouse_health === 'available' ? 'bg-emerald-400' : 'bg-amber-400'"></span>
            Remote ClickHouse host
          </h4>
          <p class="mt-1 text-xs text-slate-400">Current host resources for the configured ClickHouse target.</p>
        </div>
        <time v-if="metrics?.sampled_at" class="text-[11px] text-slate-500" :datetime="metrics.sampled_at">
          Sampled {{ sampledAtLabel }}
        </time>
      </div>

      <div class="mt-4 flex flex-wrap gap-2 text-[11px]">
        <span class="rounded-full border px-2.5 py-1" :class="healthClasses(metrics?.ssh_health)">SSH · {{ healthLabel(metrics?.ssh_health) }}</span>
        <span class="rounded-full border px-2.5 py-1" :class="healthClasses(metrics?.clickhouse_health)">ClickHouse · {{ healthLabel(metrics?.clickhouse_health) }}</span>
      </div>

      <p v-if="metrics?.stale" role="status" class="mt-3 rounded-lg border border-amber-500/25 bg-amber-500/5 px-3 py-2 text-xs text-amber-200">
        Showing the last successful sample. {{ metrics.ssh_message }}
      </p>
      <p v-else-if="metrics?.ssh_message" role="status" class="mt-3 rounded-lg border border-rose-500/25 bg-rose-500/5 px-3 py-2 text-xs text-rose-200">
        {{ metrics.ssh_message }}
      </p>
      <p v-else-if="metrics?.clickhouse_message" role="status" class="mt-3 rounded-lg border border-amber-500/25 bg-amber-500/5 px-3 py-2 text-xs text-amber-200">
        {{ metrics.clickhouse_message }}
      </p>
      <p v-else-if="!metrics && firstRequestPending" role="status" class="mt-4 text-xs text-slate-400">
        Connecting to the remote host and collecting its first sample…
      </p>
      <p v-else-if="!metrics" role="status" class="mt-4 text-xs text-slate-400">
        Waiting for the first remote host sample.
      </p>

      <div v-if="metrics?.metrics" class="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <div class="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
          <p class="text-[10px] font-semibold uppercase tracking-wide text-slate-500">CPU</p>
          <p class="mt-2 font-mono text-lg font-semibold text-slate-100">
            <span v-if="metrics.metrics.cpu_percent !== null">{{ metrics.metrics.cpu_percent.toFixed(1) }}%</span>
            <span v-else class="text-sm text-slate-400">Warming up</span>
          </p>
        </div>
        <div class="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
          <p class="text-[10px] font-semibold uppercase tracking-wide text-slate-500">RAM</p>
          <p class="mt-2 font-mono text-sm font-semibold text-slate-100">
            {{ formatBytes(metrics.metrics.memory_used_bytes) }} / {{ formatBytes(metrics.metrics.memory_total_bytes) }}
          </p>
        </div>
        <div class="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
          <p class="text-[10px] font-semibold uppercase tracking-wide text-slate-500">Load average · 1 / 5 / 15 min</p>
          <p class="mt-2 font-mono text-sm font-semibold text-slate-100">
            {{ metrics.metrics.load_average.map((value) => value.toFixed(2)).join(' / ') }}
          </p>
        </div>
        <div class="rounded-xl border border-slate-800 bg-slate-950/50 p-3 sm:col-span-2 xl:col-span-1">
          <p class="text-[10px] font-semibold uppercase tracking-wide text-slate-500">ClickHouse data path · free / total</p>
          <p class="mt-2 font-mono text-sm font-semibold text-slate-100">
            {{ formatBytes(metrics.metrics.disk_free_bytes) }} / {{ formatBytes(metrics.metrics.disk_total_bytes) }}
          </p>
          <p class="mt-1 font-mono text-[10px] text-slate-500">/var/lib/clickhouse</p>
        </div>
      </div>
    </section>
  </Transition>
</template>
