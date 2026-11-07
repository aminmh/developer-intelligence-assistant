<script setup lang="ts">
import { ExternalLink } from 'lucide-vue-next';
import { outlineHref, symbolLabel, type ImpactResult } from './ruleKnowledge';

defineProps<{ result: ImpactResult; instanceUrl: string | null }>();
const verdictLabel = { may_break: 'May break the rule', changes_behaviour: 'Changes rule behaviour', unclear: 'Check by hand', unaffected: 'Unaffected' } as const;
const verdictClass = { may_break: 'bg-rose-500/10 text-rose-200 border-rose-500/30', changes_behaviour: 'bg-amber-500/10 text-amber-200 border-amber-500/30',
  unclear: 'bg-slate-700/40 text-slate-200 border-slate-600', unaffected: 'bg-emerald-500/10 text-emerald-200 border-emerald-500/30' } as const;
</script>

<template>
  <div class="space-y-2" aria-live="polite">
    <p class="text-xs text-slate-400">
      Changed code: <template v-if="result.changed_symbols.length"><code v-for="(symbol, i) in result.changed_symbols.slice(0, 6)" :key="symbol">{{ i ? ', ' : '' }}{{ symbolLabel(symbol) }}</code><template v-if="result.changed_symbols.length > 6"> and {{ result.changed_symbols.length - 6 }} more</template></template>
      <template v-else>no indexed method matched the changed lines</template>
    </p>
    <p v-if="!result.reviews.length" class="text-sm text-slate-300">{{ result.reason || 'No business rule is linked to the changed code.' }}</p>
    <ul v-else class="space-y-2">
      <li v-for="review in result.reviews" :key="review.doc_id" class="rounded-lg border p-3" :class="verdictClass[review.verdict]">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <a :href="outlineHref(instanceUrl, review.url)" target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-1 font-medium hover:underline">{{ review.title }}<ExternalLink :size="12" /></a>
          <span class="text-xs font-semibold">{{ verdictLabel[review.verdict] }}<template v-if="!review.confirmed"> · unconfirmed link</template></span>
        </div>
        <p class="mt-1 text-sm text-slate-200">{{ review.reason }}</p>
        <p v-if="review.hunk_ids.length" class="mt-1 text-[11px] opacity-80">Hunks: {{ review.hunk_ids.map((id) => { const hunk = result.hunks.find((h) => h.id === id); return hunk ? `${hunk.file}:${hunk.start}` : id; }).join(', ') }}</p>
      </li>
    </ul>
  </div>
</template>
