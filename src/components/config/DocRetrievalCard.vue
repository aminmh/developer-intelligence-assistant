<script setup lang="ts">
import { ref } from 'vue';
import { BookOpen, RefreshCw } from 'lucide-vue-next';
import { RAGConfig } from '../../types';

const { config, updateField } = defineProps<{
  config: RAGConfig;
  updateField: <K extends keyof RAGConfig>(key: K, value: RAGConfig[K]) => void;
}>();

const testingDocRetrieval = ref(false);
const docRetrievalTest = ref<null | { ok: boolean; detail: string }>(null);

const clampNum = (v: string): number => {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? Math.max(1, Math.min(120, n)) : 10;
};

const testDocRetrieval = async () => {
  testingDocRetrieval.value = true;
  docRetrievalTest.value = null;
  try {
    const res = await fetch('/api/docretrieval/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        package: 'laravel/framework',
        resolved_version: '11.38.2',
        query: 'routing and blade templates',
      }),
    });
    const data = await res.json();
    if (data.success) {
      docRetrievalTest.value = {
        ok: true,
        detail: `${data.provider || 'unknown'} · ${(data.chunks || []).length} chunks` +
          (data.provenance?.length ? ` · ${data.provenance.length} pkg(s)` : ''),
      };
    } else {
      docRetrievalTest.value = {
        ok: false,
        detail: (data.warning || 'lookup returned nothing').slice(0, 200),
      };
    }
  } catch (e: any) {
    docRetrievalTest.value = { ok: false, detail: String(e?.message || e).slice(0, 200) };
  } finally {
    testingDocRetrieval.value = false;
  }
};
</script>

<template>
  <!-- ============ Documentation Retrieval ============ -->
  <div class="space-y-3">
    <div class="flex items-center gap-2">
      <span class="w-1 h-3.5 bg-indigo-500 rounded-full"></span>
      <h4 class="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Documentation Retrieval</h4>
    </div>

    <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4 max-w-2xl">
      <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center justify-between gap-2 border-b border-slate-800 pb-3">
        <span class="flex items-center gap-2">
          <BookOpen class="w-4 h-4 text-indigo-400" />
          Version-aware Library Docs
        </span>
        <button
          type="button"
          @click="testDocRetrieval"
          class="text-[10px] text-slate-400 hover:text-indigo-300 flex items-center gap-1 font-mono"
          :disabled="testingDocRetrieval"
        >
          <RefreshCw class="w-3 h-3" :class="testingDocRetrieval ? 'animate-spin' : ''" />
          {{ testingDocRetrieval ? 'Probing…' : 'test lookup' }}
        </button>
      </h4>

      <div class="flex items-center justify-between gap-3">
        <span class="text-xs text-slate-400 font-mono">Enable doc grounding (tool jobs)</span>
        <button
          type="button"
          role="switch"
          :aria-checked="!!config.docRetrievalEnabled"
          @click="updateField('docRetrievalEnabled', !config.docRetrievalEnabled)"
          class="relative w-9 h-5 rounded-full transition-colors disabled:opacity-50"
          :class="config.docRetrievalEnabled ? 'bg-indigo-500' : 'bg-slate-700'"
        >
          <span
            class="absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all"
            :class="config.docRetrievalEnabled ? 'left-[18px]' : 'left-0.5'"
          ></span>
        </button>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-4 pt-1">
        <div class="flex items-center justify-between gap-2 border border-slate-800 rounded-xl px-3 py-2">
          <div>
            <div class="text-xs text-slate-300 font-mono">Context7</div>
            <div class="text-[9px] text-slate-500 font-mono">primary provider</div>
          </div>
          <button
            type="button"
            role="switch"
            :aria-checked="!!config.docContext7Enabled"
            @click="updateField('docContext7Enabled', !config.docContext7Enabled)"
            class="relative w-8 h-4.5 rounded-full transition-colors shrink-0"
            :class="config.docContext7Enabled ? 'bg-indigo-500' : 'bg-slate-700'"
          >
            <span
              class="absolute top-0.5 w-3.5 h-3.5 rounded-full bg-white transition-all"
              :class="config.docContext7Enabled ? 'left-[16px]' : 'left-0.5'"
            ></span>
          </button>
        </div>

        <div class="flex items-center justify-between gap-2 border border-slate-800 rounded-xl px-3 py-2">
          <div>
            <div class="text-xs text-slate-300 font-mono">GitMCP fallback</div>
            <div class="text-[9px] text-slate-500 font-mono">llms.txt / GitHub docs</div>
          </div>
          <button
            type="button"
            role="switch"
            :aria-checked="!!config.docGitmcpEnabled"
            @click="updateField('docGitmcpEnabled', !config.docGitmcpEnabled)"
            class="relative w-8 h-4.5 rounded-full transition-colors shrink-0"
            :class="config.docGitmcpEnabled ? 'bg-indigo-500' : 'bg-slate-700'"
          >
            <span
              class="absolute top-0.5 w-3.5 h-3.5 rounded-full bg-white transition-all"
              :class="config.docGitmcpEnabled ? 'left-[16px]' : 'left-0.5'"
            ></span>
          </button>
        </div>
      </div>

      <div class="flex items-center justify-between gap-3">
        <div>
          <div class="text-xs text-slate-300 font-mono">Strict mode</div>
          <div class="text-[9px] text-slate-500 font-mono">stop when docs cannot be verified</div>
        </div>
        <button
          type="button"
          role="switch"
          :aria-checked="!!config.docStrictMode"
          @click="updateField('docStrictMode', !config.docStrictMode)"
          class="relative w-8 h-4.5 rounded-full transition-colors shrink-0"
          :class="config.docStrictMode ? 'bg-amber-500' : 'bg-slate-700'"
        >
          <span
            class="absolute top-0.5 w-3.5 h-3.5 rounded-full bg-white transition-all"
            :class="config.docStrictMode ? 'left-[16px]' : 'left-0.5'"
          ></span>
        </button>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-4 pt-1">
        <div class="space-y-1.5">
          <label class="text-xs text-slate-400 font-mono">Context7 timeout (s)</label>
          <input
            :value="config.docContext7TimeoutS"
            @input="updateField('docContext7TimeoutS', clampNum(($event.target as HTMLInputElement).value))"
            type="number"
            min="1"
            max="60"
            class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-indigo-500 text-slate-200"
          />
        </div>
        <div class="space-y-1.5">
          <label class="text-xs text-slate-400 font-mono">GitMCP timeout (s)</label>
          <input
            :value="config.docGitmcpTimeoutS"
            @input="updateField('docGitmcpTimeoutS', clampNum(($event.target as HTMLInputElement).value))"
            type="number"
            min="1"
            max="120"
            class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-indigo-500 text-slate-200"
          />
        </div>
      </div>

      <p class="text-[9px] text-slate-600">
        Set <code class="text-slate-400">CONTEXT7_API_KEY</code> as an environment variable to
        use authenticated Context7 lookups; otherwise the public endpoint is used. Raw docs are
        never included verbatim in answers — they ground the model only.
      </p>

      <div v-if="docRetrievalTest" class="text-[10px] font-mono rounded-lg border border-slate-800 px-3 py-2 break-all bg-slate-900/60"
        :class="docRetrievalTest.ok ? 'text-emerald-300' : 'text-rose-300'">
        {{ docRetrievalTest.ok ? '✓ lookup ok' : '✗ lookup failed' }}
        <span class="text-slate-500">—</span>
        {{ docRetrievalTest.detail }}
      </div>
    </div>
  </div>
</template>
