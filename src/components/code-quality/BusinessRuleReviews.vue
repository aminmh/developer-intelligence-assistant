<script setup lang="ts">
import { Eye, Scale } from 'lucide-vue-next';
type BusinessRule = { rule_id: string; version: number; title: string; statement: string; category: string; source?: 'outline'; updated_at?: string; content_hash?: string };
type Review = { rule_id: string; version: number; status: 'supported' | 'concern' | 'unknown'; reason: string; evidence_ids: string[] };
const props = defineProps<{ rules: BusinessRule[]; reviews: Review[]; evidence: Array<{ evidence_id: string; file: string | null; line: number | null; category: string }> }>();
const emit = defineEmits<{ 'view-source': [file: string, line: number] }>();
const labels: Record<string, string> = { security: 'Security', clean_code: 'Clean code', maintainability: 'Maintainability', reliability: 'Reliability', performance: 'Performance' };
labels.business_rule = 'Outline business rule';
const statusLabel = { supported: 'Supported', concern: 'Concern', unknown: 'Unknown' } as const;
const statusTone = { supported: 'ok', concern: 'bad', unknown: 'warn' } as const;
const reviewFor = (rule: BusinessRule) => props.reviews.find((item) => item.rule_id === rule.rule_id && item.version === rule.version);
const statusOf = (rule: BusinessRule) => reviewFor(rule)?.status || 'unknown';
const evidenceFor = (id: string) => props.evidence.find((item) => item.evidence_id === id);
const openEvidenceSource = (id: string) => {
  const item = evidenceFor(id);
  if (item?.file) emit('view-source', item.file, item.line || 1);
};
</script>

<template>
  <section v-if="rules.length" class="cq-card" aria-labelledby="business-rule-review-title">
    <div class="cq-sec-head">
      <h3 id="business-rule-review-title" class="cq-sec-title"><Scale aria-hidden="true" />Business rules</h3>
      <span class="cq-tag" data-tone="ok">{{ rules.length }} reviewed</span>
    </div>
    <p class="cq-hint">The model compared each rule with cited evidence from this revision. Unknown means the code evidence couldn't confirm the behaviour.</p>
    <div class="cq-rules">
      <article v-for="rule in rules" :key="`${rule.rule_id}:${rule.version}`" class="cq-rule" :data-status="statusOf(rule)">
        <div class="cq-rule-top">
          <h4>{{ rule.title }}</h4>
          <span class="cq-tag" :data-tone="statusTone[statusOf(rule)]">{{ statusLabel[statusOf(rule)] }}</span>
        </div>
        <p class="cq-rule-statement">{{ rule.statement }}</p>
        <p class="cq-rule-reason">{{ reviewFor(rule)?.reason || "The evaluator didn't return a result for this rule." }}</p>
        <div v-if="reviewFor(rule)?.evidence_ids?.length" class="cq-rule-cites" aria-label="Business rule evidence citations">
          <template v-for="evidenceId in reviewFor(rule)?.evidence_ids" :key="evidenceId">
            <a :href="`#${evidenceId}`" class="cq-link">{{ evidenceFor(evidenceId)?.file ? `${evidenceFor(evidenceId)?.file}:${evidenceFor(evidenceId)?.line || 1}` : `Evidence ${evidenceId.slice(-6)}` }}</a>
            <button v-if="evidenceFor(evidenceId)?.file" type="button" class="cq-btn cq-btn-sm" :aria-label="`View source for ${evidenceFor(evidenceId)?.file}, line ${evidenceFor(evidenceId)?.line || 1}`" @click="openEvidenceSource(evidenceId)"><Eye aria-hidden="true" />Source</button>
          </template>
        </div>
        <p class="cq-rule-meta">{{ labels[rule.category] || rule.category }} · {{ rule.source === 'outline' ? 'Outline document' : 'Legacy local rule' }} {{ rule.rule_id }}<span v-if="rule.content_hash"> · content {{ rule.content_hash.slice(0, 12) }}</span><span v-if="rule.updated_at"> · updated {{ rule.updated_at }}</span></p>
      </article>
    </div>
  </section>
</template>
