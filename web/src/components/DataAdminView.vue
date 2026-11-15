<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import {
  ChevronRight, Eye, HardDrive, Lock, RefreshCw, ShieldCheck, Trash2, UserPlus, X,
} from 'lucide-vue-next';
import { makeRequest } from '../ui/makeRequest';
import { useUrlTab } from '../composables/useUrlTab';
import DeletionDialog from './data-admin/DeletionDialog.vue';
import {
  STORE_LABELS, STORE_UNITS, formatBytes, formatCount, readJson,
  type DeletionTarget, type InventoryItem, type StoreName, type StoreSummary,
} from './data-admin/types';

type Panel = 'overview' | 'projects' | 'stores' | 'orphans' | 'audit' | 'admins';
const STORES = Object.keys(STORE_LABELS) as StoreName[];

const panel = useUrlTab<Panel>({ values: ['overview', 'projects', 'stores', 'orphans', 'audit', 'admins'], fallback: 'overview' });
const globalAdmin = ref(false);
const loading = ref(false);
const error = ref('');
const summary = ref<{ stores: Record<string, StoreSummary>; orphans: number; projects: number; generated_at?: string } | null>(null);
const projects = ref<any[]>([]);
const expanded = ref(new Set<string>());
const store = ref<StoreName>('qdrant');
const storeItems = ref<InventoryItem[]>([]);
const storeStatus = ref<{ status: string; error: string | null }>({ status: 'ok', error: null });
const projectFilter = ref('');
const ownerFilter = ref('');
const orphans = ref<InventoryItem[]>([]);
const selected = ref(new Set<string>());
const audit = ref<any[]>([]);
const admins = ref<Array<{ subject: string; bootstrap: boolean }>>([]);
const knownUsers = ref<Array<{ sub: string; email?: string; name?: string }>>([]);
const newAdmin = ref('');
const preview = ref<{ item: InventoryItem; samples: any[] } | null>(null);
const previewError = ref('');
const deletion = ref<{ target: DeletionTarget; title: string } | null>(null);

const panels = computed(() => [
  { id: 'overview' as Panel, label: 'Overview' },
  { id: 'projects' as Panel, label: 'Projects' },
  { id: 'stores' as Panel, label: 'Stores' },
  ...(globalAdmin.value ? [{ id: 'orphans' as Panel, label: `Orphans${summary.value?.orphans ? ` · ${summary.value.orphans}` : ''}` }] : []),
  { id: 'audit' as Panel, label: 'Audit log' },
  ...(globalAdmin.value ? [{ id: 'admins' as Panel, label: 'Administrators' }] : []),
]);
const ownerOptions = ['revision', 'project', 'shared', 'legacy', 'orphan'];
const itemKey = (item: InventoryItem) => `${item.store}\u0000${item.id}`;
const listForSelection = computed(() => (panel.value === 'orphans' ? orphans.value : storeItems.value));
const selectedItems = computed(() => listForSelection.value.filter((item) => selected.value.has(itemKey(item))));
const deletableCount = computed(() => listForSelection.value.filter((item) => item.deletable).length);
const largestProjects = computed(() => [...projects.value]
  .map((row) => ({ ...row, total: STORES.reduce((sum, name) => sum + (row.stores[name]?.count || 0), 0) }))
  .sort((a, b) => b.total - a.total).slice(0, 6));

const run = async (task: () => Promise<void>) => {
  loading.value = true;
  error.value = '';
  try { await task(); } catch (err: any) { error.value = err.message; } finally { loading.value = false; }
};

const loadOverview = (refresh = false) => run(async () => {
  const query = refresh ? '?refresh=true' : '';
  const [summaryData, projectData] = await Promise.all([
    readJson(await makeRequest(`/api/data-admin/summary${query}`)),
    readJson(await makeRequest(`/api/data-admin/projects${query}`)),
  ]);
  summary.value = summaryData;
  globalAdmin.value = Boolean(summaryData.globalAdmin);
  projects.value = projectData.projects || [];
});
const loadStore = () => run(async () => {
  const query = new URLSearchParams();
  if (projectFilter.value) query.set('project_id', projectFilter.value);
  if (ownerFilter.value) query.set('owner', ownerFilter.value);
  const data = await readJson(await makeRequest(`/api/data-admin/stores/${store.value}/items?${query}`));
  storeItems.value = data.items || [];
  storeStatus.value = { status: data.status, error: data.error };
});
const loadOrphans = () => run(async () => {
  orphans.value = (await readJson(await makeRequest('/api/data-admin/orphans'))).items || [];
});
const loadAudit = () => run(async () => {
  audit.value = (await readJson(await makeRequest('/api/data-admin/audit'))).entries || [];
});
const loadAdmins = () => run(async () => {
  const data = await readJson(await makeRequest('/api/auth/global-admins'));
  admins.value = data.admins || [];
  knownUsers.value = data.known_users || [];
});
const loadPanel = () => {
  selected.value = new Set();
  if (panel.value === 'stores') return loadStore();
  if (panel.value === 'orphans') return loadOrphans();
  if (panel.value === 'audit') return loadAudit();
  if (panel.value === 'admins') return loadAdmins();
  return undefined;
};

const refreshAll = async () => { await loadOverview(true); await loadPanel(); };

const toggleProject = (projectId: string) => {
  const next = new Set(expanded.value);
  if (next.has(projectId)) next.delete(projectId); else next.add(projectId);
  expanded.value = next;
};
const toggleItem = (item: InventoryItem) => {
  if (!item.deletable) return;
  const next = new Set(selected.value);
  const key = itemKey(item);
  if (next.has(key)) next.delete(key); else next.add(key);
  selected.value = next;
};
const selectAllDeletable = () => {
  selected.value = new Set(listForSelection.value.filter((item) => item.deletable).map(itemKey));
};

const openPreview = async (item: InventoryItem) => {
  preview.value = { item, samples: [] };
  previewError.value = '';
  try {
    const data = await readJson(await makeRequest(`/api/data-admin/stores/${item.store}/preview?id=${encodeURIComponent(item.id)}`));
    preview.value = { item, samples: data.samples || [] };
  } catch (err: any) {
    previewError.value = err.message;
  }
};

const askDelete = (target: DeletionTarget, title: string) => { deletion.value = { target, title }; };
const deleteSelected = () => askDelete(
  { kind: 'items', items: selectedItems.value.map((item) => ({ store: item.store, id: item.id })) },
  `Delete ${selectedItems.value.length} selected item${selectedItems.value.length === 1 ? '' : 's'}?`,
);
const afterDeletion = async () => { selected.value = new Set(); await refreshAll(); };

const addAdmin = () => run(async () => {
  const subject = newAdmin.value.trim();
  if (!subject) return;
  admins.value = (await readJson(await makeRequest(`/api/auth/global-admins/${encodeURIComponent(subject)}`, { method: 'PUT' }))).admins;
  newAdmin.value = '';
});
const removeAdmin = (subject: string) => run(async () => {
  admins.value = (await readJson(await makeRequest(`/api/auth/global-admins/${encodeURIComponent(subject)}`, { method: 'DELETE' }))).admins;
});
const userLabel = (subject: string) => {
  const user = knownUsers.value.find((row) => row.sub === subject);
  return user?.email || user?.name || subject;
};
const targetLabel = (target: any) => {
  if (target?.kind === 'project') return `project ${target.project_id}`;
  if (target?.kind === 'revision') return `revision ${target.revision_id} of ${target.project_id}`;
  if (target?.kind === 'items') return `${target.items?.length || 0} item(s)`;
  return '—';
};

watch(panel, loadPanel);
watch([store, projectFilter, ownerFilter], () => { if (panel.value === 'stores') loadStore(); });
onMounted(async () => { await loadOverview(); await loadPanel(); });
</script>

<template>
  <div class="lg-page dm-page">
    <div class="lg-wrap">
      <div class="lg-head">
        <div class="lg-title">
          <h2><HardDrive />Data management</h2>
          <p>Everything this app stores, by store and by project.<template v-if="summary?.generated_at"> Scanned <b>{{ new Date(summary.generated_at).toLocaleTimeString() }}</b>.</template></p>
        </div>
        <div class="lg-actions dm-actions">
          <span class="dm-role" :data-admin="globalAdmin">
            <ShieldCheck v-if="globalAdmin" /><Lock v-else />{{ globalAdmin ? 'Full access' : 'Read-only' }}
          </span>
          <button type="button" class="lg-btn" :disabled="loading" @click="refreshAll">
            <RefreshCw :class="loading ? 'lg-spin' : ''" />Rescan
          </button>
        </div>
      </div>

      <nav class="lg-chips dm-tabs" aria-label="Data management sections">
        <button v-for="item in panels" :key="item.id" type="button" class="lg-chip" :aria-pressed="panel === item.id" @click="panel = item.id">
          {{ item.label }}
        </button>
      </nav>

      <p v-if="error" class="dm-error dm-banner" role="alert">{{ error }}</p>

      <!-- Overview -->
      <section v-if="panel === 'overview'" class="dm-section">
        <div class="dm-cards">
          <article v-for="name in STORES" :key="name" class="dm-card" :data-status="summary?.stores[name]?.status || 'ok'">
            <header><span>{{ STORE_LABELS[name] }}</span><i v-if="summary?.stores[name]?.status !== 'ok'">unavailable</i></header>
            <strong>{{ formatCount(summary?.stores[name]?.count) }}</strong>
            <small>{{ STORE_UNITS[name] }} · {{ formatCount(summary?.stores[name]?.items) }} items · {{ formatBytes(summary?.stores[name]?.bytes) }}</small>
            <small v-if="summary?.stores[name]?.error" class="dm-error">{{ summary?.stores[name]?.error }}</small>
            <button v-if="globalAdmin && summary?.stores[name]?.orphans" type="button" class="dm-link" @click="panel = 'orphans'">
              {{ summary?.stores[name]?.orphans }} orphan or legacy
            </button>
          </article>
        </div>
        <div class="dm-block">
          <h3>Largest projects</h3>
          <p v-if="!largestProjects.length" class="dm-empty">No project data yet.</p>
          <ul class="dm-bars">
            <li v-for="row in largestProjects" :key="row.project_id">
              <span>{{ row.project_id }}</span>
              <span class="dm-bar"><i :style="{ transform: `scaleX(${row.total / (largestProjects[0]?.total || 1)})` }" /></span>
              <b>{{ formatCount(row.total) }}</b>
            </li>
          </ul>
        </div>
      </section>

      <!-- Projects -->
      <section v-else-if="panel === 'projects'" class="dm-section">
        <p class="dm-hint">Unused revisions are ready revisions eligible for retention cleanup. Latest revisions, assessments, pins, Git sync and running deployments keep revisions in use or retained. Deletion rechecks active jobs and deployments.</p>
        <p v-if="!projects.length" class="dm-empty">No projects you can see hold data.</p>
        <article v-for="row in projects" :key="row.project_id" class="dm-project">
          <header>
            <button type="button" class="dm-expand" :aria-expanded="expanded.has(row.project_id)" @click="toggleProject(row.project_id)">
              <ChevronRight />{{ row.project_id }}
            </button>
            <span v-if="!row.registered" class="dm-owner" data-owner="orphan">no revisions</span>
            <span class="dm-project-stats">
              <span v-for="name in STORES" :key="name" v-show="row.stores[name]?.count">{{ STORE_LABELS[name].split(' ')[0] }} <b>{{ formatCount(row.stores[name]?.count) }}</b></span>
            </span>
            <button v-if="row.can_delete" type="button" class="lg-btn lg-btn-sm" data-variant="danger"
              @click="askDelete({ kind: 'project', project_id: row.project_id }, `Purge project ${row.project_id}?`)">
              <Trash2 />Purge project
            </button>
          </header>
          <table v-if="expanded.has(row.project_id)" class="dm-table">
            <thead><tr><th>Revision</th><th>Source</th><th>State</th><th>Usage</th><th>Created</th><th v-if="row.can_delete"></th></tr></thead>
            <tbody>
              <tr v-if="!row.revisions.length"><td colspan="6" class="dm-empty">No registered revisions.</td></tr>
              <tr v-for="revision in row.revisions" :key="revision.revision_id">
                <td class="dm-mono">{{ revision.revision_id }}</td>
                <td>{{ revision.source_kind || '—' }}</td>
                <td>{{ revision.state }}</td>
                <td>
                  <span class="dm-owner" :data-owner="revision.usage?.status === 'unused' ? 'legacy' : revision.usage?.status === 'kept' ? 'revision' : 'transient'">{{ revision.usage?.status === 'unused' ? 'Unused' : revision.usage?.status === 'kept' ? 'In use / kept' : revision.usage?.status === 'not_ready' ? 'Not ready' : 'Unknown' }}</span>
                  <p class="dm-hint">{{ revision.usage?.reasons?.join(' ') || 'Usage has not been checked.' }}</p>
                </td>
                <td>{{ revision.created_at || '—' }}</td>
                <td v-if="row.can_delete" class="dm-right">
                  <button type="button" class="lg-btn lg-btn-sm" data-variant="danger"
                    @click="askDelete({ kind: 'revision', project_id: row.project_id, revision_id: revision.revision_id }, `Remove revision ${revision.revision_id}?`)">
                    <Trash2 />Remove
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </article>
      </section>

      <!-- Stores and orphans share one table -->
      <section v-else-if="panel === 'stores' || panel === 'orphans'" class="dm-section">
        <div v-if="panel === 'stores'" class="lg-filters">
          <div class="lg-frow">
            <span class="lg-flabel">store</span>
            <div class="lg-chips" role="group" aria-label="Store">
              <button v-for="name in STORES" :key="name" type="button" class="lg-chip" :aria-pressed="store === name" @click="store = name">{{ STORE_LABELS[name] }}</button>
            </div>
          </div>
          <div class="lg-frow">
            <span class="lg-flabel">project</span>
            <select v-model="projectFilter" class="app-select dm-select" aria-label="Project">
              <option value="">All projects</option>
              <option v-for="row in projects" :key="row.project_id" :value="row.project_id">{{ row.project_id }}</option>
            </select>
            <span class="lg-flabel">owner</span>
            <div class="lg-chips" role="group" aria-label="Owner">
              <button type="button" class="lg-chip" :aria-pressed="ownerFilter === ''" @click="ownerFilter = ''">Any</button>
              <button v-for="owner in ownerOptions" :key="owner" type="button" class="lg-chip" :aria-pressed="ownerFilter === owner" @click="ownerFilter = owner">{{ owner }}</button>
            </div>
          </div>
        </div>
        <p v-if="panel === 'orphans'" class="dm-hint">Data that no registered revision or project owns: leftovers of removed revisions and older filename-keyed uploads. Recent raw uploads and workspaces are protected for 24 hours.</p>
        <p v-if="panel === 'stores' && storeStatus.status !== 'ok'" class="dm-error dm-banner">{{ STORE_LABELS[store] }} is unavailable: {{ storeStatus.error }}</p>

        <div v-if="globalAdmin && deletableCount" class="dm-selection">
          <span>{{ selectedItems.length }} of {{ deletableCount }} deletable selected</span>
          <button type="button" class="lg-btn lg-btn-sm" @click="selectAllDeletable">Select all deletable</button>
          <button type="button" class="lg-btn lg-btn-sm" :disabled="!selectedItems.length" @click="selected = new Set()">Clear</button>
          <button type="button" class="lg-btn lg-btn-sm" data-variant="danger" :disabled="!selectedItems.length" @click="deleteSelected"><Trash2 />Delete selected</button>
        </div>

        <div class="dm-table-wrap">
          <table class="dm-table">
            <thead>
              <tr>
                <th v-if="globalAdmin" class="dm-check-col"></th>
                <th>Item</th><th>Owner</th><th>Project / revision</th><th class="dm-right">Count</th><th class="dm-right">Size</th><th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!listForSelection.length"><td colspan="7" class="dm-empty">{{ loading ? 'Scanning…' : 'Nothing here.' }}</td></tr>
              <tr v-for="item in listForSelection" :key="itemKey(item)" :data-selected="selected.has(itemKey(item))">
                <td v-if="globalAdmin" class="dm-check-col">
                  <input type="checkbox" :disabled="!item.deletable" :checked="selected.has(itemKey(item))"
                    :title="item.deletable ? 'Select for deletion' : 'Remove through its revision or project'"
                    :aria-label="`Select ${item.name}`" @change="toggleItem(item)" />
                </td>
                <td>
                  <span class="dm-mono dm-name" :title="item.name">{{ item.name }}</span>
                  <small class="dm-reason">{{ panel === 'orphans' ? `${STORE_LABELS[item.store]} · ` : '' }}{{ item.reason }}</small>
                </td>
                <td><span class="dm-owner" :data-owner="item.owner">{{ item.owner }}</span></td>
                <td class="dm-mono dm-small">{{ item.project_id || '—' }}<template v-if="item.revision_id"><br />{{ item.revision_id }}</template></td>
                <td class="dm-right">{{ formatCount(item.count) }}</td>
                <td class="dm-right">{{ formatBytes(item.bytes) }}</td>
                <td class="dm-right">
                  <button type="button" class="dm-icon-btn" :aria-label="`Preview ${item.name}`" title="Preview" @click="openPreview(item)"><Eye /></button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- Audit -->
      <section v-else-if="panel === 'audit'" class="dm-section">
        <p class="dm-hint">Every dry run and deletion, newest first{{ globalAdmin ? '' : ', for your projects' }}.</p>
        <div class="dm-table-wrap">
          <table class="dm-table">
            <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Target</th><th>Result</th></tr></thead>
            <tbody>
              <tr v-if="!audit.length"><td colspan="5" class="dm-empty">No entries yet.</td></tr>
              <tr v-for="entry in audit" :key="entry.id">
                <td class="dm-small">{{ new Date(entry.at).toLocaleString() }}</td>
                <td>{{ entry.actor_email || entry.actor_sub || '—' }}</td>
                <td>{{ entry.action }}</td>
                <td>{{ targetLabel(entry.target) }}</td>
                <td><span class="dm-result" :data-result="entry.result">{{ entry.result }}</span><small v-if="entry.error" class="dm-error">{{ entry.error }}</small></td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- Administrators -->
      <section v-else-if="panel === 'admins'" class="dm-section">
        <p class="dm-hint">Global administrators have full access to every project and can delete data here. Everyone else sees their own projects, read-only.</p>
        <ul class="dm-admins">
          <li v-for="admin in admins" :key="admin.subject">
            <ShieldCheck /><span>{{ userLabel(admin.subject) }}</span><small class="dm-mono">{{ admin.subject }}</small>
            <span v-if="admin.bootstrap" class="dm-owner" data-owner="project">bootstrap</span>
            <button v-else type="button" class="dm-icon-btn" :aria-label="`Remove ${admin.subject}`" @click="removeAdmin(admin.subject)"><X /></button>
          </li>
        </ul>
        <form class="dm-add-admin" @submit.prevent="addAdmin">
          <input v-model="newAdmin" list="dm-known-users" placeholder="OIDC subject of a signed-in user" aria-label="Subject to make administrator" />
          <datalist id="dm-known-users">
            <option v-for="user in knownUsers" :key="user.sub" :value="user.sub">{{ user.email || user.name || user.sub }}</option>
          </datalist>
          <button type="submit" class="lg-btn" :disabled="!newAdmin.trim() || loading"><UserPlus />Add administrator</button>
        </form>
      </section>
    </div>

    <!-- Preview drawer -->
    <Teleport to=".app-shell">
      <Transition name="app-dialog">
        <div v-if="preview" class="dm-dialog-root">
          <div class="dm-dialog-backdrop" @click="preview = null" />
          <aside class="dm-drawer" aria-label="Item preview">
            <header class="dm-dialog-head">
              <div>
                <h3 class="dm-mono">{{ preview.item.name }}</h3>
                <p>{{ STORE_LABELS[preview.item.store] }} · {{ preview.item.owner }} · {{ preview.item.reason }}</p>
              </div>
              <button type="button" class="dm-icon-btn" aria-label="Close preview" @click="preview = null"><X /></button>
            </header>
            <pre v-if="Object.keys(preview.item.detail || {}).length" class="dm-pre">{{ JSON.stringify(preview.item.detail, null, 2) }}</pre>
            <p v-if="previewError" class="dm-error">{{ previewError }}</p>
            <h4 v-else>Samples</h4>
            <pre v-for="(sample, index) in preview.samples" :key="index" class="dm-pre">{{ JSON.stringify(sample, null, 2) }}</pre>
            <p v-if="!previewError && !preview.samples.length" class="dm-empty">No samples.</p>
          </aside>
        </div>
      </Transition>
    </Teleport>

    <DeletionDialog :target="deletion?.target || null" :title="deletion?.title || ''" @close="deletion = null" @finished="afterDeletion" />
  </div>
</template>
