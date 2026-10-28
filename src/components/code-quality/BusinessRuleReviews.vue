<script setup lang="ts">
type BusinessRule = { rule_id: string; version: number; title: string; statement: string; category: string; source?: 'outline'; updated_at?: string; content_hash?: string };
type Review = { rule_id: string; version: number; status: 'supported' | 'concern' | 'unknown'; reason: string; evidence_ids: string[] };
const props = defineProps<{ rules: BusinessRule[]; reviews: Review[]; evidence: Array<{ evidence_id: string; file: string | null; line: number | null; category: string }> }>();
const emit = defineEmits<{ 'view-source': [file: string, line: number] }>();
const labels: Record<string, string> = { security: 'Security', clean_code: 'Clean code', maintainability: 'Maintainability', reliability: 'Reliability', performance: 'Performance' };
labels.business_rule = 'Outline business rule';
const statusStyles = {
  supported: 'border-emerald-500/25 bg-emerald-500/5 text-emerald-200',
  concern: 'border-amber-500/25 bg-amber-500/5 text-amber-200',
  unknown: 'border-slate-700 bg-slate-950/40 text-slate-300',
};
const evidenceFor = (id: string) => props.evidence.find((item) => item.evidence_id === id);
const openEvidenceSource = (id: string) => {
  const item = evidenceFor(id);
  if (item?.file) emit('view-source', item.file, item.line || 1);
};
</script>

<template>
  <section v-if="rules.length" class="mt-5 rounded-2xl border border-indigo-400/20 bg-indigo-500/[0.035] p-4 sm:p-5" aria-labelledby="business-rule-review-title">
    <div><h3 id="business-rule-review-title" class="text-base font-semibold text-slate-100">Business rule review</h3><p class="mt-1 text-xs leading-relaxed text-slate-400">The model compared project requirements with cited evidence from this revision. Unknown means the available code evidence could not verify the behavior.</p></div>
    <div class="mt-4 space-y-3">
      <article v-for="rule in rules" :key="`${rule.rule_id}:${rule.version}`" class="rounded-xl border p-4" :class="statusStyles[reviews.find((item) => item.rule_id === rule.rule_id && item.version === rule.version)?.status || 'unknown']">
        <div class="flex flex-wrap items-start justify-between gap-2"><div><h4 class="font-medium text-slate-100">{{ rule.title }}</h4><p class="mt-1 text-sm leading-relaxed text-slate-300">{{ rule.statement }}</p><p class="mt-2 break-all font-mono text-[11px] text-slate-500">{{ rule.source === 'outline' ? 'Outline document' : 'Legacy local rule' }} {{ rule.rule_id }}<span v-if="rule.content_hash"> · content {{ rule.content_hash.slice(0, 12) }}</span><span v-if="rule.updated_at"> · updated {{ rule.updated_at }}</span></p></div><span class="shrink-0 rounded-full bg-slate-950/50 px-2.5 py-1 text-[11px] font-medium">{{ labels[rule.category] || rule.category }} · {{ reviews.find((item) => item.rule_id === rule.rule_id && item.version === rule.version)?.status || 'unknown' }}</span></div>
        <p class="mt-3 text-sm leading-relaxed text-slate-300">{{ reviews.find((item) => item.rule_id === rule.rule_id && item.version === rule.version)?.reason || 'The evaluator did not return a result for this rule.' }}</p>
        <div v-if="reviews.find((item) => item.rule_id === rule.rule_id && item.version === rule.version)?.evidence_ids?.length" class="mt-3 flex flex-wrap gap-2" aria-label="Business rule evidence citations">
          <div v-for="evidenceId in reviews.find((item) => item.rule_id === rule.rule_id && item.version === rule.version)?.evidence_ids" :key="evidenceId" class="flex flex-wrap items-center gap-2">
            <a :href="`#${evidenceId}`" class="rounded-md border border-indigo-400/20 bg-indigo-400/5 px-2.5 py-1.5 text-xs text-indigo-200 hover:bg-indigo-400/10">{{ evidenceFor(evidenceId)?.file ? `${evidenceFor(evidenceId)?.file}:${evidenceFor(evidenceId)?.line || 1}` : `Evidence ${evidenceId.slice(-6)}` }} · evidence details</a>
            <button v-if="evidenceFor(evidenceId)?.file" type="button" class="min-h-8 rounded-md border border-slate-700 px-2.5 text-xs text-slate-300 hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-400" :aria-label="`View source for ${evidenceFor(evidenceId)?.file}, line ${evidenceFor(evidenceId)?.line || 1}`" @click="openEvidenceSource(evidenceId)">View source</button>
          </div>
        </div>
      </article>
    </div>
  </section>
</template>
