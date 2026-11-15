<script setup lang="ts">
import { computed, ref } from 'vue';
import { Plus, X } from 'lucide-vue-next';
import { fillRoute, ms, pct, routeParameters, type RouteSuggestion, type Selection } from './performance';

// Pick routes from the revision's Runtime traffic (or add a path), and fill route parameters.
const props = defineProps<{ routes: RouteSuggestion[]; loading: boolean; reason: string; windowHours?: number | null; disabled: boolean;
  remembered?: Record<string, Record<string, string>> }>();
const selections = defineModel<Selection[]>({ required: true });
const filter = ref('');
const custom = ref('');
const customError = ref('');
const visible = computed(() => {
  const query = filter.value.trim().toLowerCase();
  return props.routes.filter((item) => !query || item.route.toLowerCase().includes(query));
});
const selected = (route: string) => selections.value.find((item) => item.template === route);
const toggle = (route: string) => {
  if (selected(route)) selections.value = selections.value.filter((item) => item.template !== route);
  else if (selections.value.length < 20) selections.value = [...selections.value, { template: route, params: { ...(props.remembered?.[route] || {}) } }];
};
const addCustom = () => {
  const path = custom.value.trim();
  customError.value = '';
  if (!path.startsWith('/') || path.startsWith('//') || /[\s\\]/.test(path) || /(^|\/)\.\.(\/|$)/.test(path)) {
    customError.value = 'Enter a path that starts with one slash, for example /api/v1/pizzas?page=2.';
    return;
  }
  if (selected(path)) { customError.value = 'That path is already selected.'; return; }
  if (selections.value.length >= 20) { customError.value = 'Select at most 20 routes.'; return; }
  selections.value = [...selections.value, { template: path, params: {}, custom: true }];
  custom.value = '';
};
const customSelections = computed(() => selections.value.filter((item) => item.custom));
</script>

<template>
  <div>
    <div class="flex items-start justify-between gap-3">
      <div>
        <h3 class="font-semibold">Routes to benchmark</h3>
        <p class="mt-1 text-xs leading-relaxed text-slate-500">GET routes this revision served in Runtime{{ windowHours ? ` (last ${windowHours} h)` : '' }}, busiest first. Nothing is requested until you start a run.</p>
      </div>
      <span class="shrink-0 text-xs text-slate-400">{{ selections.length }}/20 selected</span>
    </div>
    <input v-if="routes.length > 6" v-model="filter" class="mt-3 min-h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm" placeholder="Filter routes" aria-label="Filter routes" />
    <p v-if="loading" class="mt-3 text-sm text-slate-400">Loading routes…</p>
    <p v-else-if="!routes.length" class="mt-3 rounded-lg border border-dashed border-slate-700 p-3 text-sm text-slate-400">{{ reason || 'No GET routes were recorded for this revision yet.' }} Add a path below, or run the project with Runtime telemetry first.</p>
    <ul v-else class="mt-3 max-h-80 space-y-1.5 overflow-auto pr-1">
      <li v-for="item in visible" :key="item.route" class="rounded-lg border px-3 py-2" :class="selected(item.route) ? 'border-emerald-400/40 bg-emerald-500/5' : 'border-slate-800 bg-slate-900/30'">
        <label class="flex cursor-pointer items-start gap-3">
          <input type="checkbox" class="mt-1 h-4 w-4 accent-emerald-400" :checked="!!selected(item.route)" :disabled="disabled" @change="toggle(item.route)" />
          <span class="min-w-0 flex-1">
            <span class="block break-all font-mono text-xs text-slate-200">GET {{ item.route }}</span>
            <span class="mt-0.5 block text-[11px] text-slate-500">production: {{ item.requests.toLocaleString() }} requests · p95 {{ ms(item.p95_ms) }} · errors {{ item.error_rate != null ? `${Number(item.error_rate).toFixed(1)}%` : '—' }}</span>
          </span>
        </label>
        <div v-if="selected(item.route) && item.parameters.length" class="mt-2 flex flex-wrap items-center gap-2 pl-7">
          <label v-for="name in item.parameters" :key="name" class="flex items-center gap-1 text-[11px] text-slate-400">
            <code>{{ '{' + name + '}' }}</code>
            <input v-model="selected(item.route)!.params[name]" :disabled="disabled" class="min-h-7 w-28 rounded border border-slate-700 bg-slate-950 px-2 font-mono text-xs text-slate-100" :placeholder="`value of ${name}`" />
          </label>
          <span class="text-[11px] text-slate-500">→ <code class="text-slate-300">{{ fillRoute(selected(item.route)!) }}</code></span>
        </div>
      </li>
    </ul>
    <ul v-if="customSelections.length" class="mt-2 space-y-1.5">
      <li v-for="item in customSelections" :key="item.template" class="flex items-center gap-2 rounded-lg border border-emerald-400/40 bg-emerald-500/5 px-3 py-2">
        <span class="min-w-0 flex-1 break-all font-mono text-xs text-slate-200">GET {{ item.template }}</span>
        <template v-for="name in routeParameters(item.template)" :key="name"><input v-model="item.params[name]" class="min-h-7 w-24 rounded border border-slate-700 bg-slate-950 px-2 font-mono text-xs" :placeholder="name" /></template>
        <button type="button" class="rounded p-1 text-slate-400 hover:bg-slate-800" :aria-label="`Remove ${item.template}`" @click="selections = selections.filter((entry) => entry !== item)"><X :size="13" /></button>
      </li>
    </ul>
    <form class="mt-3 flex gap-2" @submit.prevent="addCustom">
      <input v-model="custom" :disabled="disabled" class="min-h-9 flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 font-mono text-xs" placeholder="/api/v1/pizzas?page=2" aria-label="Add a GET path" />
      <button type="submit" :disabled="disabled || !custom.trim()" class="inline-flex min-h-9 items-center gap-1 rounded-lg border border-slate-700 px-3 text-xs text-slate-300 disabled:opacity-50"><Plus :size="13" />Add path</button>
    </form>
    <p v-if="customError" role="alert" class="mt-1 text-xs text-rose-200">{{ customError }}</p>
  </div>
</template>
