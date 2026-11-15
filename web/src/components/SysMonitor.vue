<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted } from 'vue';
import { Cpu, Thermometer, Gauge, MemoryStick, Server } from 'lucide-vue-next';
import { makeRequest } from '../ui/makeRequest';

const props = defineProps<{ lowPerformanceMode: boolean }>();

const stats = ref<any>({ online: false });

const NORMAL_POLL_INTERVAL_MS = 2000;
const LOW_PERFORMANCE_POLL_INTERVAL_MS = 10000;
let pollTimer: ReturnType<typeof setInterval> | null = null;
let mounted = false;
let requestInFlight = false;

const pct = (v: unknown): number => {
  const n = Number(v);
  if (!Number.isFinite(n)) return 0;
  return Math.min(100, Math.max(0, n));
};

const gb = (bytes: unknown): string => {
  const n = Number(bytes);
  if (!Number.isFinite(n)) return '0.0';
  return (n / 1073741824).toFixed(1);
};

const load = async () => {
  if (requestInFlight) return;
  requestInFlight = true;
  try {
    const res = await makeRequest('/api/sysmon');
    const data = await res.json();
    stats.value = { online: true, ...data };
  } catch {
    stats.value = { online: false };
  } finally {
    requestInFlight = false;
  }
};

const restartPolling = () => {
  if (!mounted) return;
  if (pollTimer) clearInterval(pollTimer);
  load();
  const interval = props.lowPerformanceMode
    ? LOW_PERFORMANCE_POLL_INTERVAL_MS
    : NORMAL_POLL_INTERVAL_MS;
  pollTimer = setInterval(load, interval);
};

watch(() => props.lowPerformanceMode, restartPolling);

onMounted(() => {
  mounted = true;
  restartPolling();
});

onUnmounted(() => {
  mounted = false;
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
});
</script>

<template>
  <!-- Compact strip: one small meter per resource, no animated waves. -->
  <div class="chat-monitor" role="group" aria-label="System monitor">
    <span class="chat-meter" :title="stats.online ? 'Host agent connected' : 'Start the host agent: node scripts/sysmon-agent.mjs'">
      <span class="chat-meter-dot" :data-online="stats.online" aria-hidden="true"></span>
      <Server aria-hidden="true" />
      {{ stats.online ? 'host agent' : 'host agent offline' }}
    </span>
    <template v-if="stats.online">
      <span class="chat-meter" :title="`CPU ${pct(stats.cpuPercent).toFixed(0)}%`">
        <Cpu aria-hidden="true" />CPU
        <span class="chat-bar"><i :style="{ transform: `scaleX(${pct(stats.cpuPercent) / 100})` }"></i></span>
        {{ pct(stats.cpuPercent).toFixed(0) }}%
      </span>
      <span class="chat-meter" :title="`${gb(stats.ramUsedBytes)} of ${gb(stats.ramTotalBytes)} GB`">
        <MemoryStick aria-hidden="true" />RAM
        <span class="chat-bar"><i :style="{ transform: `scaleX(${pct(stats.ramPercent) / 100})` }"></i></span>
        {{ gb(stats.ramUsedBytes) }}/{{ gb(stats.ramTotalBytes) }} GB
      </span>
      <span class="chat-meter" :title="stats.gpuPercent == null ? 'GPU usage needs sudo on this host' : `GPU ${pct(stats.gpuPercent).toFixed(0)}%`">
        <Gauge aria-hidden="true" />GPU
        <template v-if="stats.gpuPercent != null">
          <span class="chat-bar"><i :style="{ transform: `scaleX(${pct(stats.gpuPercent) / 100})` }"></i></span>
          {{ pct(stats.gpuPercent).toFixed(0) }}%
        </template>
        <template v-else>n/a</template>
      </span>
      <span v-if="stats.tempC != null" class="chat-meter">
        <Thermometer aria-hidden="true" />{{ Number(stats.tempC).toFixed(0) }}°C
      </span>
    </template>
    <code v-else class="chat-monitor-hint">node scripts/sysmon-agent.mjs</code>
  </div>
</template>
