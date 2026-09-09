<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue';
import {
  Activity,
  Cpu,
  Thermometer,
  Gauge,
  MemoryStick,
  ChevronDown,
  ChevronUp,
  Server,
} from 'lucide-vue-next';
import { makeRequest } from '../ui/makeRequest';

const stats = ref<any>({ online: false });
const expanded = ref(true);
const polling = ref(true);

const POLL_INTERVAL_MS = 2000;
let pollTimer: ReturnType<typeof setInterval> | null = null;

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

const barColor = (v: number): string => {
  if (v < 50) return 'bg-emerald-500 shadow-[0_0_12px] shadow-emerald-500/50';
  if (v < 80) return 'bg-amber-500 shadow-[0_0_12px] shadow-amber-500/50';
  return 'bg-rose-500 shadow-[0_0_12px] shadow-rose-500/50';
};

const load = async () => {
  try {
    const res = await makeRequest('/api/sysmon');
    const data = await res.json();
    stats.value = { online: true, ...data };
  } catch {
    stats.value = { online: false };
  }
};

onMounted(() => {
  load();
  pollTimer = setInterval(load, POLL_INTERVAL_MS);
});

onUnmounted(() => {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
});
</script>

<template>
  <div class="bg-slate-900/40 border border-slate-800 rounded-2xl overflow-hidden">
    <!-- Header -->
    <div class="flex items-center gap-2 px-4 py-3 border-b border-slate-800">
      <Activity class="w-4 h-4 text-indigo-400" />
      <span class="text-xs font-bold text-slate-200 uppercase tracking-wider font-mono">System Monitor</span>
      <span
        class="w-2 h-2 rounded-full animate-pulse ml-auto"
        :class="stats.online ? 'bg-emerald-500' : 'bg-rose-500'"
      ></span>
      <button
        type="button"
        @click="expanded = !expanded"
        class="text-slate-500 hover:text-slate-300 transition-colors"
        aria-label="Toggle system monitor"
      >
        <ChevronDown v-if="expanded" class="w-4 h-4" />
        <ChevronUp v-else class="w-4 h-4" />
      </button>
    </div>

    <!-- Body -->
    <div v-if="expanded" class="p-4">
      <!-- Offline state -->
      <div
        v-if="!stats.online"
        class="flex items-center gap-3 py-6 text-center justify-center"
      >
        <Server class="w-4 h-4 text-slate-600 shrink-0" />
        <span class="text-[10px] text-slate-500 font-mono leading-relaxed">
          System monitor offline — start the host agent:
          <code class="text-slate-400">node docs/gpu-offload/scripts/sysmon-agent.mjs</code>
        </span>
      </div>

      <!-- Gauges -->
      <div v-else class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <!-- CPU -->
        <div class="bg-[#11162d] border border-slate-800 rounded-xl p-3">
          <div class="flex items-center gap-1 text-[9px] text-slate-500 font-mono mb-1.5">
            <Cpu class="w-3 h-3 text-indigo-400" />
            CPU
          </div>
          <span :key="pct(stats.cpuPercent)" class="text-lg font-bold text-slate-100 font-mono transition-opacity duration-500">
            {{ pct(stats.cpuPercent).toFixed(0) }}%
          </span>
          <div class="mt-2 h-1.5 bg-slate-800 rounded-full overflow-hidden">
            <div
              class="h-full rounded-full transition-all duration-700 ease-out"
              :class="barColor(pct(stats.cpuPercent))"
              :style="{ width: `${pct(stats.cpuPercent)}%` }"
            ></div>
          </div>
        </div>

        <!-- Memory -->
        <div class="bg-[#11162d] border border-slate-800 rounded-xl p-3">
          <div class="flex items-center gap-1 text-[9px] text-slate-500 font-mono mb-1.5">
            <MemoryStick class="w-3 h-3 text-indigo-400" />
            RAM
          </div>
          <span :key="`${gb(stats.ramUsedBytes)}-${gb(stats.ramTotalBytes)}`" class="text-lg font-bold text-slate-100 font-mono transition-opacity duration-500">
            {{ gb(stats.ramUsedBytes) }} / {{ gb(stats.ramTotalBytes) }} GB
          </span>
          <div class="mt-2 h-1.5 bg-slate-800 rounded-full overflow-hidden">
            <div
              class="h-full rounded-full transition-all duration-700 ease-out"
              :class="barColor(pct(stats.ramPercent))"
              :style="{ width: `${pct(stats.ramPercent)}%` }"
            ></div>
          </div>
        </div>

        <!-- Temperature -->
        <div class="bg-[#11162d] border border-slate-800 rounded-xl p-3">
          <div class="flex items-center gap-1 text-[9px] text-slate-500 font-mono mb-1.5">
            <Thermometer class="w-3 h-3 text-indigo-400" />
            Temp
          </div>
          <span v-if="stats.tempC != null" :key="stats.tempC" class="text-lg font-bold text-slate-100 font-mono transition-opacity duration-500">
            {{ Number(stats.tempC).toFixed(0) }}°C
          </span>
          <span v-else class="text-lg font-bold text-slate-600 font-mono">—</span>
          <div class="mt-2 h-1.5 bg-slate-800 rounded-full overflow-hidden">
            <div
              v-if="stats.tempC != null"
              class="h-full rounded-full transition-all duration-700 ease-out"
              :class="barColor(pct(stats.tempC))"
              :style="{ width: `${pct(stats.tempC)}%` }"
            ></div>
          </div>
          <div v-if="stats.tempC == null" class="mt-2 text-[9px] text-slate-600 font-mono">n/a</div>
        </div>

        <!-- GPU -->
        <div class="bg-[#11162d] border border-slate-800 rounded-xl p-3">
          <div class="flex items-center gap-1 text-[9px] text-slate-500 font-mono mb-1.5">
            <Gauge class="w-3 h-3 text-indigo-400" />
            GPU
          </div>
          <span v-if="stats.gpuPercent != null" :key="stats.gpuPercent" class="text-lg font-bold text-slate-100 font-mono transition-opacity duration-500">
            {{ pct(stats.gpuPercent).toFixed(0) }}%
          </span>
          <span v-else class="text-lg font-bold text-slate-600 font-mono">n/a</span>
          <div class="mt-2 h-1.5 bg-slate-800 rounded-full overflow-hidden">
            <div
              v-if="stats.gpuPercent != null"
              class="h-full rounded-full transition-all duration-700 ease-out"
              :class="barColor(pct(stats.gpuPercent))"
              :style="{ width: `${pct(stats.gpuPercent)}%` }"
            ></div>
          </div>
          <div v-if="stats.gpuPercent == null" class="mt-2 text-[9px] text-slate-600 font-mono">not available (needs sudo)</div>
        </div>
      </div>
    </div>
  </div>
</template>
