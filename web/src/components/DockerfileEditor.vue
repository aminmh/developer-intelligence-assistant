<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue';
import * as monaco from 'monaco-editor';
import { makeRequest } from '../ui/makeRequest';

const props = defineProps<{ modelValue: string }>();
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();
const host = ref<HTMLElement | null>(null);
const unavailable = ref(false);
const diagnostics = ref(0);
let editor: monaco.editor.IStandaloneCodeEditor | null = null;
let model: monaco.editor.ITextModel | null = null;
let sessionId = '';
let version = 0;
let timer: ReturnType<typeof setTimeout> | null = null;
let changeDisposable: monaco.IDisposable | null = null;

const uri = `dockerfile://editor-${Math.random().toString(36).slice(2)}`;
const open = async () => {
  if (!model) return;
  version += 1;
  try {
    const res = await makeRequest('/api/dockerfile/language/open', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sessionId: sessionId || undefined, uri, version, text: model.getValue() }) });
    const data = await res.json();
    if (!res.ok || !data.success) throw new Error(data.error || 'unavailable');
    sessionId = data.sessionId; unavailable.value = false;
    const items = data.diagnostics || [];
    diagnostics.value = items.length;
    monaco.editor.setModelMarkers(model, 'dockerfile-language-server', items.map((d: any) => ({ startLineNumber: d.range.start.line + 1, startColumn: d.range.start.character + 1, endLineNumber: d.range.end.line + 1, endColumn: Math.max(d.range.end.character + 1, d.range.start.character + 2), message: d.message, severity: d.severity === 1 ? monaco.MarkerSeverity.Error : d.severity === 2 ? monaco.MarkerSeverity.Warning : monaco.MarkerSeverity.Info, source: d.source, code: d.code })));
  } catch { unavailable.value = true; }
};
const scheduleOpen = () => { if (timer) clearTimeout(timer); timer = setTimeout(open, 350); };

onMounted(() => {
  if (!host.value) return;
  model = monaco.editor.createModel(props.modelValue, 'dockerfile');
  editor = monaco.editor.create(host.value, { model, theme: 'vs-dark', automaticLayout: true, minimap: { enabled: false }, fontSize: 12, lineNumbers: 'on', padding: { top: 8, bottom: 8 } });
  changeDisposable = model.onDidChangeContent(() => { emit('update:modelValue', model!.getValue()); scheduleOpen(); });
  monaco.languages.registerCompletionItemProvider('dockerfile', { triggerCharacters: [' ', '\\n'], provideCompletionItems: async (_m, position) => {
    if (!sessionId) await open();
    if (!sessionId) return { suggestions: [] };
    try {
      const res = await makeRequest('/api/dockerfile/language/complete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sessionId, position: { line: position.lineNumber - 1, character: position.column - 1 } }) });
      const data = await res.json();
      return { suggestions: (data.items || []).map((item: any) => ({ label: item.label, kind: monaco.languages.CompletionItemKind.Keyword, insertText: item.insertText || item.label, detail: item.detail, documentation: item.documentation?.value || item.documentation })) };
    } catch { return { suggestions: [] }; }
  }});
  open();
});

watch(() => props.modelValue, (value) => { if (model && value !== model.getValue()) model.setValue(value); });
onBeforeUnmount(async () => { if (timer) clearTimeout(timer); changeDisposable?.dispose(); editor?.dispose(); model?.dispose(); if (sessionId) await makeRequest(`/api/dockerfile/language/${encodeURIComponent(sessionId)}`, { method: 'DELETE' }).catch(() => undefined); });

const formatDocument = async () => {
  if (!sessionId) await open();
  if (!sessionId || !model) return;
  try {
    const res = await makeRequest('/api/dockerfile/language/format', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sessionId }) });
    const data = await res.json();
    if (!res.ok || !data.success) return;
    const edits = (data.edits || []).map((edit: any) => ({ range: new monaco.Range(edit.range.start.line + 1, edit.range.start.character + 1, edit.range.end.line + 1, edit.range.end.character + 1), text: edit.newText }));
    editor?.executeEdits('dockerfile-format', edits);
  } catch { unavailable.value = true; }
};
</script>

<template>
  <div class="flex-1 min-w-[280px] space-y-1">
    <div class="flex items-center justify-between text-[10px] text-slate-500">
      <span v-if="unavailable" class="text-amber-300">Dockerfile intelligence unavailable — editor remains usable.</span>
      <span v-else-if="diagnostics" class="text-amber-300">{{ diagnostics }} diagnostic{{ diagnostics === 1 ? '' : 's' }}</span>
      <span v-else>Dockerfile editor</span>
      <button type="button" class="px-2 py-1 rounded border border-slate-700 hover:border-indigo-500" @click="formatDocument">Format</button>
    </div>
    <div ref="host" class="h-44 w-full rounded border border-slate-800 overflow-hidden" />
  </div>
</template>
