import { computed, ref } from 'vue';
import { makeRequest } from '../ui/makeRequest';
import type { RevisionView } from '../utils/chatRevisions';

/** A revision as the project switcher sees it. `indexed` is false when its vectors are missing. */
export interface ScopeRevision extends RevisionView {
  state?: string;
  indexed: boolean;
  vectors_missing?: boolean;
}

export interface ScopeProject {
  project_id: string;
  revisions: ScopeRevision[];
  indexed: boolean;
}

/** 'latest' follows the newest indexed revision; a revision ID pins one. */
export type ScopeRevisionChoice = 'latest' | string;

const STORAGE_KEY = 'dev-assistant-project-scope';
const REVISION_PATTERN = /^(latest|rev_[0-9a-f]{32})$/;

// One app-wide selection: every page reads and writes this module-level state.
const projects = ref<ScopeProject[]>([]);
const loaded = ref(false);
const loading = ref(false);
const loadError = ref('');
const projectId = ref('');
const revision = ref<ScopeRevisionChoice>('latest');
let started = false;

const readStored = (): { project: string; revision: string } => {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    return { project: typeof value.project === 'string' ? value.project : '', revision: typeof value.revision === 'string' ? value.revision : 'latest' };
  } catch {
    return { project: '', revision: 'latest' };
  }
};

const readFromUrl = () => {
  const params = new URLSearchParams(window.location.search);
  const project = params.get('project');
  if (project === null) return null;
  const rev = params.get('revision') || 'latest';
  return { project, revision: REVISION_PATTERN.test(rev) ? rev : 'latest' };
};

const persist = (push: boolean) => {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ project: projectId.value, revision: revision.value })); } catch { /* private mode */ }
  const url = new URL(window.location.href);
  if (projectId.value) url.searchParams.set('project', projectId.value); else url.searchParams.delete('project');
  if (projectId.value && revision.value !== 'latest') url.searchParams.set('revision', revision.value); else url.searchParams.delete('revision');
  if (url.href === window.location.href) return;
  if (push) window.history.pushState(window.history.state, '', url);
  else window.history.replaceState(window.history.state, '', url);
};

const applyFromUrl = () => {
  const fromUrl = readFromUrl();
  if (!fromUrl) return;
  projectId.value = fromUrl.project.trim();
  revision.value = fromUrl.revision;
};

const toScope = (raw: any[]): ScopeProject[] => raw
  .filter((item) => item && typeof item.project_id === 'string')
  .map((item) => {
    const revisions: ScopeRevision[] = (Array.isArray(item.revisions) ? item.revisions : []).map((rev: any) => ({
      ...rev,
      indexed: typeof rev.indexed === 'boolean' ? rev.indexed : (rev.state ?? 'ready') === 'ready',
    }));
    return { project_id: item.project_id, revisions, indexed: revisions.some((rev) => rev.indexed) };
  });

async function load() {
  if (loading.value) return;
  loading.value = true;
  loadError.value = '';
  try {
    let response = await makeRequest('/api/projects/scope');
    if (response.status === 404) {
      // Older API without the scope endpoint: fall back to the ready revisions chat already uses.
      response = await makeRequest('/api/chat/revisions');
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !Array.isArray(data.projects)) throw new Error(data.error || 'Projects are unavailable.');
    projects.value = toScope(data.projects);
  } catch (error: any) {
    loadError.value = error?.message || 'Projects are unavailable.';
  } finally {
    loading.value = false;
    loaded.value = true;
  }
}

function start() {
  if (started) return;
  started = true;
  const fromUrl = readFromUrl();
  const stored = fromUrl ?? readStored();
  projectId.value = stored.project.trim();
  revision.value = REVISION_PATTERN.test(stored.revision) ? stored.revision : 'latest';
  persist(false);
  window.addEventListener('popstate', applyFromUrl);
  void load();
}

/** Select a project (and optionally a revision). A project not in the list starts a new one. */
function select(nextProject: string, nextRevision: ScopeRevisionChoice = 'latest') {
  const project = nextProject.trim();
  const rev = REVISION_PATTERN.test(nextRevision) ? nextRevision : 'latest';
  if (project === projectId.value && rev === revision.value) return;
  projectId.value = project;
  revision.value = rev;
  persist(true);
}

const currentProject = computed(() => projects.value.find((item) => item.project_id === projectId.value) ?? null);
const latestIndexed = computed(() => currentProject.value?.revisions.find((item) => item.indexed) ?? null);
/** The revision answers come from: the pinned one, or the newest indexed one for 'latest'. */
const resolvedRevision = computed<ScopeRevision | null>(() => {
  if (!currentProject.value) return null;
  if (revision.value === 'latest') return latestIndexed.value;
  return currentProject.value.revisions.find((item) => item.revision_id === revision.value) ?? null;
});
const usable = computed(() => Boolean(resolvedRevision.value?.indexed));
const isNewProject = computed(() => Boolean(projectId.value) && loaded.value && !currentProject.value);

export function useProjectScope() {
  start();
  return {
    projects,
    loaded,
    loading,
    loadError,
    projectId,
    revision,
    currentProject,
    latestIndexed,
    resolvedRevision,
    usable,
    isNewProject,
    load,
    select,
  };
}
