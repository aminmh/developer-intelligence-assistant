<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { AlertTriangle, Check, ExternalLink, LoaderCircle, Scale, X } from 'lucide-vue-next';
import { outlineHref, ruleKnowledgePath, ruleRequest, symbolLabel, syncedAgo, type RuleLink, type RuleLinkState } from './ruleKnowledge';

// Shows the business rules linked to the code a developer is looking at (methods, routes or tables).
const props = withDefaults(defineProps<{ projectId: string; symbolIds?: string[]; file?: string; revisionId?: string; compact?: boolean }>(),
  { symbolIds: () => [], file: '', revisionId: '', compact: false });
const links = ref<RuleLink[]>([]);
const instanceUrl = ref<string | null>(null);
const loading = ref(false);
const error = ref('');
const saving = ref('');
let sequence = 0;

const ids = computed(() => [...new Set(props.symbolIds.filter((value) => typeof value === 'string' && value))].slice(0, 200));
const visible = computed(() => links.value.filter((link) => link.state !== 'rejected'));
const grouped = computed(() => {
  const rules = new Map<string, { doc_id: string; title: string; url: string; synced_at?: string; links: RuleLink[] }>();
  for (const link of visible.value) {
    const entry = rules.get(link.doc_id) || { doc_id: link.doc_id, title: link.title || link.doc_id, url: link.url || '', synced_at: link.synced_at, links: [] };
    entry.links.push(link);
    rules.set(link.doc_id, entry);
  }
  return [...rules.values()].sort((a, b) => Number(a.links.every((l) => l.state === 'suggested')) - Number(b.links.every((l) => l.state === 'suggested'))
    || a.title.localeCompare(b.title));
});

const load = async () => {
  const current = ++sequence;
  if (!props.projectId || (!ids.value.length && !props.file)) { links.value = []; return; }
  loading.value = true; error.value = '';
  try {
    const query = props.file ? `file=${encodeURIComponent(props.file)}`
      : ids.value.map((value) => `symbol_id=${encodeURIComponent(value)}`).join('&');
    const data = await ruleRequest<{ links: RuleLink[]; instance_url: string | null }>(ruleKnowledgePath(props.projectId, `/links?${query}`));
    if (current !== sequence) return;
    links.value = Array.isArray(data.links) ? data.links : [];
    instanceUrl.value = data.instance_url;
  } catch (cause: any) {
    if (current === sequence) error.value = cause?.message || 'Business rules are unavailable.';
  } finally { if (current === sequence) loading.value = false; }
};

const setState = async (link: RuleLink, state: RuleLinkState) => {
  const key = `${link.doc_id}:${link.symbol_id}`;
  saving.value = key; error.value = '';
  try {
    const data = await ruleRequest<{ link: RuleLink }>(ruleKnowledgePath(props.projectId, '/links'), {
      method: 'PUT', json: { doc_id: link.doc_id, symbol_id: link.symbol_id, state, ...(props.revisionId ? { revision_id: props.revisionId } : {}) },
    });
    links.value = links.value.map((item) => item.doc_id === link.doc_id && item.symbol_id === link.symbol_id ? { ...item, ...data.link } : item);
  } catch (cause: any) { error.value = cause?.message || 'The link could not be saved.'; }
  finally { saving.value = ''; }
};

watch(() => [props.projectId, props.file, ids.value.join('\n')], () => void load(), { immediate: true });
defineExpose({ reload: load });
</script>

<template>
  <section class="rounded-lg border border-slate-800 bg-slate-950/40" :class="compact ? 'p-2.5' : 'p-3'" aria-label="Business rules for this code">
    <h4 class="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-slate-400"><Scale :size="13" />Business rules</h4>
    <p v-if="loading && !links.length" class="mt-2 flex items-center gap-1.5 text-xs text-slate-500"><LoaderCircle :size="12" class="motion-safe:animate-spin" />Looking up rules…</p>
    <p v-else-if="error" role="alert" class="mt-2 text-xs text-rose-200">{{ error }}</p>
    <p v-else-if="!grouped.length" class="mt-2 text-xs text-slate-500">No business rule is linked to this code. Link rules from Knowledge Base → Business rules.</p>
    <ul v-else class="mt-2 space-y-2">
      <li v-for="rule in grouped" :key="rule.doc_id" class="rounded-md border border-slate-800 bg-slate-900/60 p-2">
        <div class="flex items-start justify-between gap-2">
          <a :href="outlineHref(instanceUrl, rule.url)" target="_blank" rel="noopener noreferrer" class="inline-flex min-w-0 items-center gap-1 text-sm font-medium text-indigo-200 hover:underline"><span class="truncate">{{ rule.title }}</span><ExternalLink :size="12" class="shrink-0" /></a>
          <span class="shrink-0 text-[11px] text-slate-500">{{ syncedAgo(rule.synced_at) }}</span>
        </div>
        <ul class="mt-1.5 space-y-1">
          <li v-for="link in rule.links" :key="link.symbol_id" class="flex flex-wrap items-center gap-1.5 text-xs">
            <code class="max-w-full truncate text-slate-300" :title="link.symbol_id">{{ symbolLabel(link.symbol_id) }}</code>
            <span class="rounded-full px-1.5 py-0.5 text-[10px]" :class="link.state === 'suggested' ? 'bg-slate-700/60 text-slate-300' : 'bg-emerald-500/10 text-emerald-200'">{{ link.state === 'suggested' ? 'suggested' : 'confirmed' }}</span>
            <span v-if="link.rule_changed" class="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-200"><AlertTriangle :size="10" />rule changed</span>
            <span v-if="link.code_changed" class="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-200"><AlertTriangle :size="10" />code changed</span>
            <span class="ml-auto inline-flex gap-1">
              <button v-if="link.state === 'suggested' || link.needs_review" type="button" :disabled="!!saving" class="inline-flex min-h-6 items-center gap-1 rounded border border-emerald-500/30 px-1.5 text-[11px] text-emerald-200 hover:bg-emerald-500/10 disabled:opacity-50" :aria-label="`Confirm ${rule.title} for ${link.symbol_id}`" @click="setState(link, 'confirmed')"><Check :size="11" />{{ link.needs_review ? 'Still holds' : 'Confirm' }}</button>
              <button type="button" :disabled="!!saving" class="inline-flex min-h-6 items-center gap-1 rounded border border-slate-700 px-1.5 text-[11px] text-slate-400 hover:bg-slate-800 disabled:opacity-50" :aria-label="`Remove link between ${rule.title} and ${link.symbol_id}`" @click="setState(link, 'rejected')"><X :size="11" />Not related</button>
            </span>
          </li>
        </ul>
      </li>
    </ul>
  </section>
</template>
