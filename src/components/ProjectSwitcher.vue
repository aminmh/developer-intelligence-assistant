<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue';
import { Check, ChevronsUpDown, Link2, LoaderCircle, Plus, RefreshCw, Search, TriangleAlert, Users } from 'lucide-vue-next';
import { useProjectScope, type ScopeProject, type ScopeRevision } from '../composables/useProjectScope';
import { describeRevision, relativeAge } from '../utils/chatRevisions';
import { validateUploadIdentity } from '../services/uploadIdentity';

defineProps<{ canManageAccess?: boolean }>();
const emit = defineEmits<{ (e: 'openAccess'): void }>();

const scope = useProjectScope();
const open = ref(false);
const query = ref('');
const browsing = ref(''); // project shown in the revision column
const root = ref<HTMLElement | null>(null);
const trigger = ref<HTMLButtonElement | null>(null);
const searchInput = ref<HTMLInputElement | null>(null);
const dialogId = `project-switcher-${useId()}`;

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase();
  return needle ? scope.projects.value.filter((item) => item.project_id.toLowerCase().includes(needle)) : scope.projects.value;
});
const browsingProject = computed<ScopeProject | null>(() =>
  scope.projects.value.find((item) => item.project_id === browsing.value) ?? null);
const newProjectId = computed(() => {
  const value = query.value.trim();
  if (!value || scope.projects.value.some((item) => item.project_id === value)) return '';
  return value;
});
const newProjectError = computed(() => newProjectId.value ? validateUploadIdentity(newProjectId.value, '').projectError : null);

const triggerTitle = computed(() => scope.projectId.value || 'Choose a project');
const triggerDetail = computed(() => {
  if (!scope.projectId.value) return scope.projects.value.length ? `${scope.projects.value.length} projects available` : 'No projects yet';
  if (scope.isNewProject.value) return 'New project · no revisions yet';
  const resolved = scope.resolvedRevision.value;
  if (!resolved || !resolved.indexed) return scope.revision.value === 'latest' ? 'No indexed revision' : 'Revision not indexed';
  return `${scope.revision.value === 'latest' ? 'latest' : 'pinned'} · ${describeRevision(resolved)}`;
});
const triggerTone = computed(() => {
  if (!scope.projectId.value || scope.isNewProject.value) return undefined;
  return scope.usable.value ? (scope.revision.value === 'latest' ? 'latest' : 'pinned') : 'warn';
});

const revisionHint = (rev: ScopeRevision) => {
  if (rev.vectors_missing) return 'Indexed data is missing. Re-index it from Knowledge base.';
  if (!rev.indexed) return rev.state === 'failed' ? 'Indexing failed' : 'Not indexed yet';
  return '';
};
const sourceLabel = (rev: ScopeRevision) => rev.source_kind === 'git' ? 'Git sync' : rev.source_kind === 'working_tree' ? 'Working tree' : 'Upload';

const show = async () => {
  open.value = true;
  query.value = '';
  browsing.value = scope.projectId.value || scope.projects.value[0]?.project_id || '';
  if (!scope.loaded.value || scope.loadError.value) void scope.load();
  await nextTick();
  searchInput.value?.focus();
};
const hide = (focusTrigger = false) => {
  open.value = false;
  if (focusTrigger) trigger.value?.focus();
};
const choose = (projectId: string, revision: string = 'latest') => {
  scope.select(projectId, revision);
  hide(true);
};
const chooseNew = () => {
  if (!newProjectId.value || newProjectError.value) return;
  choose(newProjectId.value);
};
const onSearchKeydown = (event: KeyboardEvent) => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  const exact = scope.projects.value.find((item) => item.project_id === query.value.trim());
  if (exact) choose(exact.project_id);
  else if (filtered.value.length === 1 && !newProjectId.value) choose(filtered.value[0].project_id);
  else chooseNew();
};
// Keep the revision column on a visible project while filtering.
watch(filtered, (list) => {
  if (list.length && !list.some((item) => item.project_id === browsing.value)) browsing.value = list[0].project_id;
});

const onKeydown = (event: KeyboardEvent) => {
  if (open.value && event.key === 'Escape') { event.preventDefault(); hide(true); }
};
const onPointerDown = (event: PointerEvent) => {
  if (open.value && root.value && !root.value.contains(event.target as Node)) hide();
};
onMounted(() => { document.addEventListener('pointerdown', onPointerDown); document.addEventListener('keydown', onKeydown); });
onBeforeUnmount(() => { document.removeEventListener('pointerdown', onPointerDown); document.removeEventListener('keydown', onKeydown); });
</script>

<template>
  <div ref="root" class="ps">
    <button
      ref="trigger"
      type="button"
      class="ps-trigger"
      aria-haspopup="dialog"
      :aria-expanded="open"
      :aria-controls="dialogId"
      :title="`Project: ${triggerTitle}`"
      @click="open ? hide() : show()"
    >
      <span class="ps-avatar" :data-empty="!scope.projectId.value">{{ (scope.projectId.value || '?').slice(0, 1).toUpperCase() }}</span>
      <span class="ps-name">
        <b>{{ triggerTitle }}</b>
        <span :data-tone="triggerTone">{{ triggerDetail }}</span>
      </span>
      <ChevronsUpDown class="ps-chev" aria-hidden="true" />
    </button>

    <Transition name="ps-pop">
      <div v-if="open" :id="dialogId" class="ps-pop" role="dialog" aria-label="Choose project and revision">
        <div class="ps-search">
          <Search aria-hidden="true" />
          <input
            ref="searchInput"
            v-model="query"
            type="text"
            autocomplete="off"
            spellcheck="false"
            maxlength="128"
            placeholder="Find or type a new project ID…"
            aria-label="Find a project or type a new project ID"
            @keydown="onSearchKeydown"
          />
          <button type="button" class="ps-icon-btn" :disabled="scope.loading.value" title="Refresh projects" aria-label="Refresh projects" @click="scope.load()">
            <RefreshCw :class="{ 'ps-spin': scope.loading.value }" aria-hidden="true" />
          </button>
        </div>

        <div class="ps-body">
          <div class="ps-col" role="listbox" aria-label="Projects">
            <p class="ps-label">projects</p>
            <p v-if="scope.loading.value && !scope.projects.value.length" class="ps-empty"><LoaderCircle class="ps-spin" aria-hidden="true" />Loading projects…</p>
            <p v-else-if="scope.loadError.value && !scope.projects.value.length" class="ps-empty" data-tone="warn"><TriangleAlert aria-hidden="true" />{{ scope.loadError.value }}</p>
            <p v-else-if="!filtered.length && !newProjectId" class="ps-empty">No projects yet. Type an ID to start one.</p>
            <button
              v-for="project in filtered"
              :key="project.project_id"
              type="button"
              class="ps-proj"
              role="option"
              :aria-selected="project.project_id === scope.projectId.value"
              :data-browsing="project.project_id === browsing"
              @click="choose(project.project_id)"
              @pointerenter="browsing = project.project_id"
              @focus="browsing = project.project_id"
            >
              <i class="ps-dot" :data-off="!project.indexed" :title="project.indexed ? 'Has an indexed revision' : 'No indexed revision'"></i>
              <span>{{ project.project_id }}</span>
              <small>{{ project.revisions.length }}</small>
            </button>
            <button v-if="newProjectId" type="button" class="ps-new" :disabled="!!newProjectError" @click="chooseNew">
              <Plus aria-hidden="true" />
              <span>Start new project <b>{{ newProjectId }}</b><em v-if="newProjectError">{{ newProjectError }}</em></span>
            </button>
          </div>

          <div class="ps-col" role="listbox" :aria-label="browsingProject ? `${browsingProject.project_id} revisions` : 'Revisions'">
            <template v-if="browsingProject">
              <p class="ps-label">{{ browsingProject.project_id }} · revisions</p>
              <button
                type="button"
                class="ps-rev"
                role="option"
                :aria-selected="browsingProject.project_id === scope.projectId.value && scope.revision.value === 'latest'"
                :disabled="!browsingProject.indexed"
                :title="browsingProject.indexed ? 'Always use the newest indexed revision' : 'This project has no indexed revision yet'"
                @click="choose(browsingProject.project_id, 'latest')"
              >
                <span class="ps-radio" aria-hidden="true"></span>
                <span class="ps-rev-text">
                  <b>latest</b>
                  <span>{{ browsingProject.revisions.find((rev) => rev.indexed) ? describeRevision(browsingProject.revisions.find((rev) => rev.indexed)!) : 'No indexed revision yet' }}</span>
                </span>
                <span class="ps-tag" data-tone="follow">follows newest</span>
              </button>
              <button
                v-for="rev in browsingProject.revisions"
                :key="rev.revision_id"
                type="button"
                class="ps-rev"
                role="option"
                :aria-selected="browsingProject.project_id === scope.projectId.value && scope.revision.value === rev.revision_id"
                :disabled="!rev.indexed"
                :title="revisionHint(rev) || 'Pin this revision'"
                @click="choose(browsingProject.project_id, rev.revision_id)"
              >
                <span class="ps-radio" aria-hidden="true"></span>
                <span class="ps-rev-text">
                  <b>{{ describeRevision({ ...rev, created_at: null }) }}</b>
                  <span>{{ sourceLabel(rev) }}<template v-if="relativeAge(rev.created_at)"> · {{ relativeAge(rev.created_at) }}</template></span>
                </span>
                <span v-if="rev.indexed" class="ps-tag" data-tone="ok"><Check aria-hidden="true" />Indexed</span>
                <span v-else class="ps-tag" data-tone="warn"><TriangleAlert aria-hidden="true" />{{ rev.vectors_missing ? 'Data missing' : 'Not indexed' }}</span>
              </button>
            </template>
            <p v-else class="ps-empty">{{ newProjectId ? 'A new project has no revisions until you upload or sync one.' : 'Choose a project to see its revisions.' }}</p>
          </div>
        </div>

        <div class="ps-foot">
          <Link2 aria-hidden="true" />
          <span>Every page follows this choice.</span>
          <button v-if="canManageAccess && scope.projectId.value" type="button" class="ps-access" @click="hide(); emit('openAccess')">
            <Users aria-hidden="true" />Project access
          </button>
        </div>
      </div>
    </Transition>
  </div>
</template>
