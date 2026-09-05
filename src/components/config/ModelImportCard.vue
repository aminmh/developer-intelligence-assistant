<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue';
import { FolderSearch, Upload, RefreshCw, CheckCircle, AlertTriangle } from 'lucide-vue-next';
import { OllamaImportFile, OllamaImportJob } from '../../types';
import { makeRequest } from '../../ui/makeRequest';

const importRoot = ref('');
const ggufFiles = ref<OllamaImportFile[]>([]);
const selectedGgufFile = ref('');
const importModelName = ref('');
const importModelfile = ref('');
const scanningImportDirectory = ref(false);
const startingImport = ref(false);
const importJob = ref<OllamaImportJob | null>(null);
const importError = ref('');
const importSuccess = ref('');
let importPollTimer: ReturnType<typeof setTimeout> | null = null;

const scanImportDirectory = async () => {
  scanningImportDirectory.value = true;
  importError.value = '';
  importSuccess.value = '';
  try {
    const res = await makeRequest('/api/ollama/import-directory');
    const data = await res.json();
    if (!res.ok || !data.success) throw new Error(data.error || 'Unable to scan the import directory.');
    importRoot.value = data.root || '';
    ggufFiles.value = (data.files || []).map((file: string) => ({ file }));
    if (!selectedGgufFile.value || !data.files.includes(selectedGgufFile.value)) {
      selectedGgufFile.value = data.files[0] || '';
    }
    if (!data.files.length) importError.value = 'No .gguf files were found in the configured directory.';
  } catch (error: any) {
    importError.value = error?.message || 'Unable to scan the import directory.';
  } finally {
    scanningImportDirectory.value = false;
  }
};

const stopImportPolling = () => {
  if (importPollTimer) {
    clearTimeout(importPollTimer);
    importPollTimer = null;
  }
};

const pollImport = async (jobId: string) => {
  try {
    const res = await makeRequest(`/api/ollama/import/${encodeURIComponent(jobId)}`);
    const data = await res.json();
    if (!res.ok || !data.success) throw new Error(data.error || 'Unable to read import status.');
    importJob.value = data.job;
    if (data.job.status === 'pending' || data.job.status === 'running') {
      importPollTimer = setTimeout(() => pollImport(jobId), 1200);
    } else if (data.job.status === 'success') {
      importSuccess.value = `${data.job.modelName} is now available in Ollama.`;
      // LlmProvidersCard listens for this to refresh its model dropdowns.
      window.dispatchEvent(new CustomEvent('ollama-models-changed'));
    } else {
      importError.value = data.job.error || data.job.message || 'Ollama model import failed.';
    }
  } catch (error: any) {
    importError.value = error?.message || 'Unable to read import status.';
  }
};

const startImport = async () => {
  if (!selectedGgufFile.value || !importModelName.value.trim()) {
    importError.value = 'Select a GGUF file and enter an Ollama model name.';
    return;
  }
  stopImportPolling();
  startingImport.value = true;
  importError.value = '';
  importSuccess.value = '';
  importJob.value = null;
  try {
    const res = await makeRequest('/api/ollama/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file: selectedGgufFile.value,
        modelName: importModelName.value.trim(),
        modelfile: importModelfile.value,
      }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) throw new Error(data.error || 'Unable to start Ollama import.');
    importJob.value = data.job;
    importPollTimer = setTimeout(() => pollImport(data.jobId), 500);
  } catch (error: any) {
    importError.value = error?.message || 'Unable to start Ollama import.';
  } finally {
    startingImport.value = false;
  }
};

onMounted(scanImportDirectory);
onUnmounted(stopImportPolling);
</script>

<template>
  <!-- ============ Model Import ============ -->
  <div class="space-y-3">
    <div class="flex items-center gap-2">
      <span class="w-1 h-3.5 bg-sky-500 rounded-full"></span>
      <h4 class="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Model Import</h4>
    </div>

    <!-- GGUF import -->
    <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
      <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center justify-between gap-2 border-b border-slate-800 pb-3">
        <span class="flex items-center gap-2">
          <FolderSearch class="w-4 h-4 text-indigo-400" />
          Import GGUF Model
        </span>
        <button
          type="button"
          @click="scanImportDirectory"
          class="text-[10px] text-slate-400 hover:text-indigo-300 flex items-center gap-1 font-mono"
          :disabled="scanningImportDirectory"
        >
          <RefreshCw class="w-3 h-3" :class="scanningImportDirectory ? 'animate-spin' : ''" />
          scan
        </button>
      </h4>

      <p class="text-[10px] leading-relaxed text-slate-500">
        GGUF files must be placed in the shared directory configured for this deployment.
        The app uploads the selected file to Ollama and reports progress here.
      </p>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div class="space-y-1.5">
          <label class="text-xs text-slate-400 font-mono">Configured directory</label>
          <input
            :value="importRoot"
            readonly
            class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-[10px] font-mono text-slate-500"
            placeholder="Scan to load configured directory"
          />
        </div>

        <div class="space-y-1.5">
          <label class="text-xs text-slate-400 font-mono">Ollama model name</label>
          <input
            v-model="importModelName"
            type="text"
            class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-indigo-500 text-slate-200"
            placeholder="e.g. my-coder-model"
          />
        </div>
      </div>

      <div class="space-y-1.5">
        <label class="text-xs text-slate-400 font-mono">GGUF file</label>
        <select
          v-model="selectedGgufFile"
          class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-indigo-500 text-slate-200"
          :disabled="!ggufFiles.length"
        >
          <option value="" disabled>Select a GGUF file</option>
          <option v-for="entry in ggufFiles" :key="entry.file" :value="entry.file">{{ entry.file }}</option>
        </select>
      </div>

      <div class="space-y-1.5">
        <label class="text-xs text-slate-400 font-mono">Optional Modelfile settings</label>
        <textarea
          v-model="importModelfile"
          rows="4"
          class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-[10px] font-mono focus:outline-none focus:border-indigo-500 text-slate-200 resize-y"
          placeholder="SYSTEM You are a helpful coding assistant&#10;PARAMETER temperature 0.2"
        />
        <p class="text-[9px] text-slate-600">The app inserts the selected GGUF as the FROM source automatically.</p>
      </div>

      <button
        type="button"
        @click="startImport"
        :disabled="startingImport || importJob?.status === 'pending' || importJob?.status === 'running' || !ggufFiles.length"
        class="w-full md:w-auto flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed text-white font-semibold text-xs px-6 py-2.5 rounded-xl transition-all"
      >
        <Upload class="w-3.5 h-3.5" />
        {{ startingImport ? 'Starting…' : importJob?.status === 'running' ? 'Importing…' : 'Import model' }}
      </button>

      <div v-if="importJob && (importJob.status === 'pending' || importJob.status === 'running')" class="flex items-center gap-2 text-[10px] text-indigo-300 font-mono">
        <RefreshCw class="w-3.5 h-3.5 animate-spin" />
        <span>{{ importJob.message }}</span>
      </div>
      <div v-if="importSuccess" class="flex items-start gap-2 text-[10px] text-emerald-300 font-mono">
        <CheckCircle class="w-3.5 h-3.5 shrink-0" />
        <span>{{ importSuccess }}</span>
      </div>
      <div v-if="importError" class="flex items-start gap-2 text-[10px] text-amber-300 font-mono">
        <AlertTriangle class="w-3.5 h-3.5 shrink-0" />
        <span>{{ importError }}</span>
      </div>
    </div>
  </div>
</template>
