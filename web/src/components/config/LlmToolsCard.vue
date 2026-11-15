<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { Plus, Save, Trash2, Wrench, ShieldCheck, ShieldAlert, ChevronDown, ChevronUp, Wand2 } from 'lucide-vue-next';
import { TOOL_WORKFLOWS, parseSchemaText, schemaFromUrlTemplate } from '../../utils/agentTools';

type Effect = 'read_only' | 'side_effecting';
type AgentAccess = {
  callable: boolean;
  reason: string | null;
  effect: Effect;
  workflows: string[];
  input_schema: Record<string, unknown> | null;
};
type Tool = {
  id: string;
  type: 'shell' | 'mcp' | 'rest' | 'builtin' | 'pipeline';
  name: string;
  description: string;
  enabled: boolean;
  builtin?: boolean;
  config: Record<string, any>;
  agent?: AgentAccess;
};

const tools = ref<Tool[]>([]);
const selected = ref<Tool | null>(null);
const expandedBuiltin = ref<string | null>(null);
const bodyText = ref('{}');
const schemaText = ref('');
const hostsText = ref('');
const effect = ref<Effect>('side_effecting');
const allWorkflows = ref(true);
const workflows = ref<string[]>([]);
const error = ref('');
const message = ref('');
const testing = ref(false);
const saving = ref(false);

const builtins = computed(() => tools.value.filter((t) => t.builtin));
const managed = computed(() => tools.value.filter((t) => !t.builtin));
const schemaError = computed(() => parseSchemaText(schemaText.value).error);

const blank = (): Tool => ({ id: '', type: 'rest', name: '', description: '', enabled: true,
  config: { method: 'GET', url: '', timeoutSeconds: 30, body: {} } });

async function load() {
  const r = await fetch('/api/tools');
  const d = await r.json().catch(() => ({}));
  if (d.success) tools.value = d.tools;
  else error.value = d.error || 'Failed to load tools';
}

function openEditor(tool: Tool) {
  selected.value = JSON.parse(JSON.stringify(tool));
  const cfg = selected.value!.config;
  bodyText.value = JSON.stringify(cfg.body || {}, null, 2);
  schemaText.value = cfg.inputSchema ? JSON.stringify(cfg.inputSchema, null, 2) : '';
  hostsText.value = Array.isArray(cfg.allowedHosts) ? cfg.allowedHosts.join(', ') : (cfg.allowedHosts || '');
  effect.value = cfg.effect === 'read_only' ? 'read_only' : 'side_effecting';
  allWorkflows.value = !Array.isArray(cfg.workflows);
  workflows.value = Array.isArray(cfg.workflows) ? [...cfg.workflows] : TOOL_WORKFLOWS.map((w) => w.id);
  message.value = '';
  error.value = '';
}

function add() {
  openEditor(blank());
}

function updateType() {
  if (!selected.value) return;
  selected.value.config = selected.value.type === 'shell'
    ? { command: '', timeoutSeconds: 30 }
    : selected.value.type === 'mcp'
      ? { command: '', args: [], timeoutSeconds: 30 }
      : { method: 'GET', url: '', timeoutSeconds: 30, body: {} };
  bodyText.value = '{}';
}

function generateSchema() {
  if (!selected.value) return;
  schemaText.value = JSON.stringify(schemaFromUrlTemplate(selected.value.config.url || ''), null, 2);
}

async function save() {
  error.value = '';
  message.value = '';
  if (!selected.value?.name) {
    error.value = 'A name is required.';
    return;
  }
  const config: Record<string, any> = { ...selected.value.config };
  if (selected.value.type === 'rest') {
    try {
      config.body = JSON.parse(bodyText.value || '{}');
    } catch {
      error.value = 'REST body must be valid JSON';
      return;
    }
    config.allowedHosts = hostsText.value.split(',').map((h) => h.trim()).filter(Boolean);
  }
  const parsed = parseSchemaText(schemaText.value);
  if (parsed.error) {
    error.value = parsed.error;
    return;
  }
  if (parsed.schema) config.inputSchema = parsed.schema;
  else delete config.inputSchema;
  config.effect = effect.value;
  if (allWorkflows.value) delete config.workflows;
  else config.workflows = workflows.value;

  saving.value = true;
  try {
    const method = selected.value.id ? 'PUT' : 'POST';
    const url = selected.value.id ? `/api/tools/${selected.value.id}` : '/api/tools';
    const payload = { type: selected.value.type, name: selected.value.name, description: selected.value.description,
      enabled: selected.value.enabled, config };
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const d = await r.json().catch(() => ({}));
    if (!d.success) {
      error.value = d.detail || d.error || 'Save failed';
      return;
    }
    const access: AgentAccess | undefined = d.tool?.agent;
    message.value = access?.callable
      ? `Saved. The model can use ${d.tool.name}${access.effect === 'side_effecting' ? ' after you approve each call' : ''}.`
      : `Saved, but the model cannot use it yet: ${access?.reason || 'unknown reason'}`;
    selected.value = null;
    await load();
  } finally {
    saving.value = false;
  }
}

async function toggle(tool: Tool) {
  await fetch(`/api/tools/${tool.id}/${tool.enabled ? 'disable' : 'enable'}`, { method: 'POST' });
  await load();
}

async function remove(tool: Tool) {
  if (tool.builtin || !confirm(`Remove ${tool.name}?`)) return;
  await fetch(`/api/tools/${tool.id}`, { method: 'DELETE' });
  if (selected.value?.id === tool.id) selected.value = null;
  await load();
}

async function test() {
  if (!selected.value?.id) return;
  testing.value = true;
  try {
    const r = await fetch(`/api/tools/${selected.value.id}/test`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ filename: 'project.zip' }),
    });
    const d = await r.json().catch(() => ({}));
    message.value = d.success ? 'Test succeeded' : (d.result?.error || 'Test failed');
  } finally {
    testing.value = false;
  }
}

const typeLabel = (type: Tool['type']) => ({ builtin: 'built-in', pipeline: 'pipeline', rest: 'REST', mcp: 'MCP', shell: 'shell' }[type] || type);
const schemaPreview = (schema: Record<string, unknown> | null | undefined) => JSON.stringify(schema ?? {}, null, 2);

onMounted(load);
</script>

<template>
  <section class="rounded-2xl border border-slate-800 bg-slate-900/40 p-5 space-y-4">
    <div class="flex items-center justify-between">
      <div>
        <h3 class="text-lg font-bold text-white flex items-center gap-2"><Wrench class="w-5 h-5 text-cyan-400" /> LLM Tools</h3>
        <p class="text-xs text-slate-400 mt-1">Tools the external model can call while answering. Read-only tools run directly; tools that change things wait for your approval each time.</p>
      </div>
      <button class="px-3 py-2 rounded-lg bg-indigo-600 text-white text-xs font-semibold" @click="add"><Plus class="w-3.5 h-3.5 inline mr-1" />Add tool</button>
    </div>
    <p v-if="error" class="text-xs text-rose-300" role="alert">{{ error }}</p>
    <p v-if="message" class="text-xs text-emerald-300" role="status">{{ message }}</p>

    <!-- Editor -->
    <div v-if="selected" class="rounded-xl border border-indigo-500/30 bg-slate-950/40 p-4 space-y-3">
      <div class="grid grid-cols-2 gap-3">
        <label class="text-xs text-slate-400">Name<input v-model="selected.name" class="field font-mono" placeholder="create_ticket" /></label>
        <label class="text-xs text-slate-400">Type
          <select v-model="selected.type" class="field" @change="updateType">
            <option value="rest">REST API</option>
            <option value="mcp">MCP (stdio)</option>
            <option value="shell">Shell command</option>
          </select>
        </label>
      </div>
      <label class="text-xs text-slate-400">Description <span class="text-slate-500">(the model reads this to decide when to call the tool)</span>
        <input v-model="selected.description" class="field" placeholder="Create an issue-tracker ticket." />
      </label>
      <label v-if="selected.type === 'shell' || selected.type === 'mcp'" class="text-xs text-slate-400">Command<input v-model="selected.config.command" class="field font-mono" placeholder="php artisan route:list" /></label>
      <template v-if="selected.type === 'rest'">
        <div class="grid grid-cols-[8rem_1fr] gap-3">
          <label class="text-xs text-slate-400">Method
            <select v-model="selected.config.method" class="field"><option>GET</option><option>POST</option><option>PUT</option><option>PATCH</option><option>DELETE</option></select>
          </label>
          <label class="text-xs text-slate-400">URL template<input v-model="selected.config.url" class="field font-mono" placeholder="https://api.example.com/items/{{id}}" /></label>
        </div>
        <label class="text-xs text-slate-400">Allowed hosts <span class="text-slate-500">(comma-separated; empty allows only the URL's own host)</span>
          <input v-model="hostsText" class="field font-mono" placeholder="api.example.com" />
        </label>
        <label class="text-xs text-slate-400">JSON body<textarea v-model="bodyText" rows="3" class="field font-mono" /></label>
      </template>

      <fieldset class="rounded-lg border border-slate-800 p-3 space-y-3">
        <legend class="px-1 text-xs font-semibold text-slate-300">Model access</legend>
        <p v-if="selected.type !== 'rest'" class="text-[11px] text-amber-300/80">
          {{ selected.type === 'mcp' ? 'MCP' : 'Shell' }} tools need the isolated tool runner, which is not available yet. They stay usable in the orchestrator pipeline.
        </p>
        <div class="space-y-1.5" role="radiogroup" aria-label="What the tool does">
          <label class="flex items-start gap-2 text-xs text-slate-300">
            <input v-model="effect" type="radio" value="read_only" class="mt-0.5 accent-emerald-400" />
            <span><span class="font-semibold text-emerald-300">Read-only</span> — only reads data; the model calls it without asking.</span>
          </label>
          <label class="flex items-start gap-2 text-xs text-slate-300">
            <input v-model="effect" type="radio" value="side_effecting" class="mt-0.5 accent-amber-400" />
            <span><span class="font-semibold text-amber-300">Changes things</span> — creates, updates, sends, or deletes; you approve every call.</span>
          </label>
        </div>
        <div class="space-y-1.5">
          <label class="flex items-center gap-2 text-xs text-slate-300">
            <input v-model="allWorkflows" type="checkbox" class="accent-indigo-400" /> Allowed in every kind of question
          </label>
          <div v-if="!allWorkflows" class="flex flex-wrap gap-x-4 gap-y-1 pl-5">
            <label v-for="w in TOOL_WORKFLOWS" :key="w.id" class="flex items-center gap-1.5 text-[11px] text-slate-400">
              <input v-model="workflows" type="checkbox" :value="w.id" class="accent-indigo-400" /> {{ w.label }}
            </label>
          </div>
        </div>
        <label class="block text-xs text-slate-400">Argument schema <span class="text-slate-500">(JSON Schema the model's arguments must match; required for the model to use the tool)</span>
          <textarea v-model="schemaText" rows="6" class="field font-mono" :aria-invalid="!!schemaError" placeholder='{"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}' />
        </label>
        <div class="flex items-center gap-3">
          <button v-if="selected.type === 'rest'" type="button" class="flex items-center gap-1 text-[11px] text-cyan-300 hover:text-cyan-200" @click="generateSchema">
            <Wand2 class="w-3 h-3" /> Generate from URL placeholders
          </button>
          <span v-if="schemaError" class="text-[11px] text-rose-300">{{ schemaError }}</span>
        </div>
      </fieldset>

      <div class="flex gap-2">
        <button class="px-3 py-2 rounded-lg bg-indigo-600 text-white text-xs disabled:opacity-50" :disabled="saving || !!schemaError" @click="save"><Save class="w-3.5 h-3.5 inline mr-1" />{{ saving ? 'Saving…' : 'Save' }}</button>
        <button v-if="selected.id" class="px-3 py-2 rounded-lg border border-slate-700 text-xs text-slate-300" :disabled="testing" @click="test">{{ testing ? 'Testing…' : 'Test tool' }}</button>
        <button class="px-3 py-2 rounded-lg border border-slate-700 text-xs text-slate-400" @click="selected = null">Cancel</button>
      </div>
    </div>

    <!-- Your tools -->
    <div class="space-y-2">
      <h4 class="text-xs font-semibold uppercase tracking-wide text-slate-500">Your tools</h4>
      <p v-if="!managed.length" class="text-xs text-slate-500">No tools yet. Add a REST tool to let the model call your own APIs.</p>
      <div v-for="tool in managed" :key="tool.id" class="flex items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-950/30 p-3">
        <button class="text-left min-w-0 flex-1" @click="openEditor(tool)">
          <div class="flex items-center gap-2 text-sm text-white font-semibold">
            <span class="truncate font-mono">{{ tool.name }}</span>
            <span class="text-[10px] uppercase text-cyan-300">{{ typeLabel(tool.type) }}</span>
            <span v-if="tool.agent?.effect === 'side_effecting'" class="flex items-center gap-0.5 text-[10px] text-amber-300"><ShieldAlert class="w-3 h-3" />needs approval</span>
            <span v-else class="flex items-center gap-0.5 text-[10px] text-emerald-300"><ShieldCheck class="w-3 h-3" />read-only</span>
          </div>
          <div class="text-xs text-slate-500 truncate">{{ tool.description || 'No description' }}</div>
          <div class="text-[11px] mt-0.5" :class="tool.agent?.callable ? 'text-emerald-400/80' : 'text-slate-500'">
            {{ tool.agent?.callable ? 'Available to the model' : `Not available to the model — ${tool.agent?.reason || 'unknown reason'}` }}
          </div>
        </button>
        <div class="flex items-center gap-2 shrink-0">
          <button class="text-[11px] text-slate-400 hover:text-white" @click="toggle(tool)">{{ tool.enabled ? 'Disable' : 'Enable' }}</button>
          <button class="text-rose-300" :aria-label="`Remove ${tool.name}`" @click="remove(tool)"><Trash2 class="w-3.5 h-3.5" /></button>
        </div>
      </div>
    </div>

    <!-- Built-in tools -->
    <div class="space-y-2">
      <h4 class="text-xs font-semibold uppercase tracking-wide text-slate-500">Built-in tools</h4>
      <div v-for="tool in builtins" :key="tool.id" class="rounded-xl border border-slate-800 bg-slate-950/30">
        <button class="w-full flex items-center gap-3 p-3 text-left" :aria-expanded="expandedBuiltin === tool.id" @click="expandedBuiltin = expandedBuiltin === tool.id ? null : tool.id">
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2 text-sm text-white font-semibold">
              <span class="truncate font-mono">{{ tool.name }}</span>
              <span class="text-[10px] uppercase text-cyan-300">{{ typeLabel(tool.type) }}</span>
              <span v-if="tool.agent?.callable" class="flex items-center gap-0.5 text-[10px] text-emerald-300"><ShieldCheck class="w-3 h-3" />read-only</span>
            </div>
            <div class="text-xs text-slate-500 truncate">{{ tool.description }}</div>
            <div v-if="!tool.agent?.callable" class="text-[11px] mt-0.5 text-slate-500">{{ tool.agent?.reason }}</div>
          </div>
          <ChevronUp v-if="expandedBuiltin === tool.id" class="w-3.5 h-3.5 text-slate-500" />
          <ChevronDown v-else class="w-3.5 h-3.5 text-slate-500" />
        </button>
        <div v-if="expandedBuiltin === tool.id" class="border-t border-slate-800 px-3 pb-3 pt-2 space-y-1.5">
          <p class="text-xs text-slate-400">{{ tool.description }}</p>
          <p v-if="tool.agent?.callable" class="text-[11px] text-slate-500">Allowed in every kind of question. Built-in tools cannot be edited or removed.</p>
          <pre v-if="tool.agent?.input_schema" class="max-h-48 overflow-auto rounded bg-slate-950 p-2 text-[10px] leading-4 text-slate-400">{{ schemaPreview(tool.agent.input_schema) }}</pre>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>.field{display:block;width:100%;margin-top:.35rem;border-radius:.6rem;border:1px solid rgb(51 65 85);background:#0f172a;color:#e2e8f0;padding:.55rem .7rem;font-size:.75rem}.field:focus{outline:2px solid rgb(99 102 241 / .45)}</style>
