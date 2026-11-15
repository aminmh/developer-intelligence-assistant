<script setup lang="ts">
/**
 * UI map of a front-end project: route -> component -> child components -> stores -> HTTP requests.
 * Plan: .archcore/frontend-assist/frontend-developer-assistance.plan.md (Phase 2)
 */
import { computed, ref, watch } from 'vue';
import { Activity, Boxes, CircleAlert, CircleCheck, Database, LayoutPanelLeft, LoaderCircle, Route, Search, ShieldCheck, Waypoints } from 'lucide-vue-next';
import { makeRequest } from '../ui/makeRequest';
import { useProjectScope } from '../composables/useProjectScope';
import {
  CHECK_AREAS, componentForScreen, findingsByFile, frameworkLabel, matches, milliseconds, requestsOf, routesOf, storeKindLabel, usedBy, vitalTone,
  type CheckArea, type FrontendChecks, type ScreenViewDetail, type ScreenViews, type UiComponent, type UiMap,
} from '../utils/uiMap';

const scope = useProjectScope();
const map = ref<UiMap | null>(null);
const loading = ref(false);
const error = ref('');
const search = ref('');
const appRoot = ref('');
const selectedId = ref('');
type Section = 'routes' | 'components' | 'requests' | 'checks' | 'live';
const section = ref<Section>('routes');
const checks = ref<FrontendChecks | null>(null);
const checksLoading = ref(false);
const checksError = ref('');
const area = ref<CheckArea>('accessibility');
const live = ref<ScreenViews | null>(null);
const liveLoading = ref(false);
const liveRoute = ref('');
const liveDetail = ref<ScreenViewDetail | null>(null);

const projectId = computed(() => scope.projectId.value.trim());
const revisionId = computed(() => scope.resolvedRevision.value?.revision_id || '');

let sequence = 0;
const load = async () => {
  const current = ++sequence;
  map.value = null; error.value = ''; selectedId.value = ''; checks.value = null; checksError.value = '';
  live.value = null; liveRoute.value = ''; liveDetail.value = null;
  if (!projectId.value || !revisionId.value) return;
  loading.value = true;
  try {
    const query = new URLSearchParams({ revision_id: revisionId.value });
    if (appRoot.value) query.set('app', appRoot.value);
    const response = await makeRequest(`/api/projects/${encodeURIComponent(projectId.value)}/ui-map?${query}`);
    const data = await response.json().catch(() => ({}));
    if (current !== sequence) return;
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The UI map could not be loaded.');
    map.value = data;
    if (!map.value?.routes.length && map.value?.components.length) section.value = 'components';
  } catch (cause: any) {
    if (current === sequence) error.value = cause?.message || 'The UI map could not be loaded.';
  } finally {
    if (current === sequence) loading.value = false;
  }
};
watch([projectId, revisionId], () => { appRoot.value = ''; void load(); }, { immediate: true });
const chooseApp = (root: string) => { appRoot.value = root; void load(); };

// Checks are read when their tab is first opened: they are a separate pass over the source.
const loadChecks = async () => {
  if (checks.value || checksLoading.value || !projectId.value || !revisionId.value) return;
  const current = sequence;
  checksLoading.value = true; checksError.value = '';
  try {
    const response = await makeRequest(`/api/projects/${encodeURIComponent(projectId.value)}/frontend-checks?revision_id=${revisionId.value}`);
    const data = await response.json().catch(() => ({}));
    if (current !== sequence) return;
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The checks could not be loaded.');
    checks.value = data;
    area.value = CHECK_AREAS.find((item) => data.by_area[item.id] > 0)?.id || 'accessibility';
  } catch (cause: any) {
    if (current === sequence) checksError.value = cause?.message || 'The checks could not be loaded.';
  } finally {
    checksLoading.value = false;
  }
};
const openChecks = () => { section.value = 'checks'; void loadChecks(); };

// Live: screen views reported by the browser snippet. Read on demand; the data changes as people use the app.
const loadLive = async () => {
  if (liveLoading.value || !projectId.value) return;
  const current = sequence;
  liveLoading.value = true;
  try {
    const response = await makeRequest(`/api/projects/${encodeURIComponent(projectId.value)}/screens`);
    const data = await response.json().catch(() => ({}));
    if (current !== sequence) return;
    live.value = response.ok ? data : { available: false, reason: typeof data.detail === 'string' ? data.detail : 'Screen views could not be loaded.', views: [], screens: [] };
    if (!liveRoute.value && live.value?.screens.length) liveRoute.value = live.value.screens[0].route;
  } catch {
    if (current === sequence) live.value = { available: false, reason: 'Screen views could not be loaded.', views: [], screens: [] };
  } finally {
    liveLoading.value = false;
  }
};
const openLive = () => { section.value = 'live'; liveDetail.value = null; void loadLive(); };
const liveScreens = computed(() => (live.value?.screens || []).filter((screen) => matches(search.value, screen.route)));
const liveViews = computed(() => (live.value?.views || []).filter((view) => view.route === liveRoute.value));
const liveComponent = computed(() => componentForScreen(map.value, liveRoute.value));
const openView = async (traceId: string) => {
  liveDetail.value = null;
  try {
    const response = await makeRequest(`/api/projects/${encodeURIComponent(projectId.value)}/screens/${traceId}`);
    liveDetail.value = response.ok ? await response.json() : { available: false, reason: 'The screen view could not be loaded.' };
  } catch { liveDetail.value = { available: false, reason: 'The screen view could not be loaded.' }; }
};
const time = (value: string) => { const date = new Date(`${value.replace(' ', 'T').slice(0, 23)}Z`); return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString(); };
const checkGroups = computed(() => checks.value ? findingsByFile(checks.value, area.value, search.value) : []);

const componentsById = computed(() => new Map((map.value?.components || []).map((component) => [component.id, component])));
const storesById = computed(() => new Map((map.value?.stores || []).map((store) => [store.id, store])));
const selected = computed<UiComponent | null>(() => componentsById.value.get(selectedId.value) || null);

const routes = computed(() => (map.value?.routes || []).filter((route) => matches(search.value, route.path, route.component, route.file)));
const components = computed(() => (map.value?.components || [])
  .filter((component) => matches(search.value, component.name, component.file, component.selector))
  .sort((a, b) => a.name.localeCompare(b.name)));
const requests = computed(() => (map.value?.http_calls || []).filter((call) => matches(search.value, call.url, call.raw, call.file, call.method)));
const counts = computed(() => ({ routes: routes.value.length, components: components.value.length, requests: requests.value.length }));

const detail = computed(() => {
  if (!map.value || !selected.value) return null;
  const component = selected.value;
  return {
    component,
    routes: routesOf(map.value, component.id),
    uses: component.uses.map((id) => componentsById.value.get(id)).filter(Boolean) as UiComponent[],
    usedBy: usedBy(map.value, component.id),
    stores: component.stores.map((id) => storesById.value.get(id)).filter(Boolean),
    requests: requestsOf(map.value, component),
  };
});
const select = (id: string | null | undefined) => { if (id && componentsById.value.has(id)) selectedId.value = id; };
/** Components that own a request: defined in the same file, or reaching it through imports. */
const ownersOf = (callId: number) => (map.value?.components || [])
  .filter((component) => component.http_calls.includes(callId) || component.via_imports.includes(callId)).slice(0, 6);
</script>

<template>
  <section class="kb-page" aria-label="UI map">
    <div class="kb-wrap um-wrap">
      <div v-if="!projectId" class="kb-empty">
        <div class="kb-empty-icon"><LayoutPanelLeft aria-hidden="true" /></div>
        <h3>Choose a project</h3>
        <p>Pick an Angular, React, or Vue project in the header to see its routes, components, stores, and requests.</p>
      </div>
      <div v-else-if="!revisionId" class="kb-empty">
        <div class="kb-empty-icon"><LayoutPanelLeft aria-hidden="true" /></div>
        <h3>No indexed revision yet</h3>
        <p>The UI map is built from an indexed revision. Upload or sync the project in the Knowledge base first.</p>
      </div>
      <p v-else-if="loading" class="kb-note" role="status"><LoaderCircle class="kb-spin" aria-hidden="true" /> Reading the project's routes and components…</p>
      <p v-else-if="error" role="alert" class="kb-rev-note" data-tone="bad"><CircleAlert aria-hidden="true" />{{ error }}</p>
      <div v-else-if="map && !map.framework" class="kb-empty">
        <div class="kb-empty-icon"><LayoutPanelLeft aria-hidden="true" /></div>
        <h3>No front-end application found</h3>
        <p>This revision has no package.json that depends on Angular, React, or Vue.</p>
      </div>

      <template v-else-if="map">
        <div class="um-top">
          <span class="kb-tag" data-tone="run">{{ frameworkLabel(map) }}</span>
          <span class="kb-stat"><b>{{ map.routes.length }}</b>routes</span>
          <span class="kb-stat"><b>{{ map.components.length }}</b>components</span>
          <span class="kb-stat"><b>{{ map.stores.length }}</b>stores</span>
          <span class="kb-stat"><b>{{ map.http_calls.length }}</b>requests</span>
          <label v-if="map.apps.length > 1" class="um-app"><span class="sr-only">Application</span>
            <select class="kb-input" :value="map.app_root" @change="chooseApp(($event.target as HTMLSelectElement).value)">
              <option v-for="app in map.apps" :key="app.root" :value="app.root">{{ app.root || '(project root)' }} · {{ app.framework }}</option>
            </select>
          </label>
          <label class="um-search"><Search aria-hidden="true" /><span class="sr-only">Filter</span>
            <input v-model="search" type="search" class="kb-input" placeholder="Filter by path, component, or file" />
          </label>
        </div>
        <p v-for="note in map.notes" :key="note" class="kb-rev-note" data-tone="warn"><CircleAlert aria-hidden="true" />{{ note }}</p>

        <div class="um-body">
          <div class="kb-panel um-list">
            <div class="kb-tabs" role="tablist" aria-label="UI map sections">
              <button type="button" role="tab" :aria-selected="section === 'routes'" @click="section = 'routes'"><Route aria-hidden="true" />Routes ({{ counts.routes }})</button>
              <button type="button" role="tab" :aria-selected="section === 'components'" @click="section = 'components'"><Boxes aria-hidden="true" />Components ({{ counts.components }})</button>
              <button type="button" role="tab" :aria-selected="section === 'requests'" @click="section = 'requests'"><Waypoints aria-hidden="true" />Requests ({{ counts.requests }})</button>
              <button type="button" role="tab" :aria-selected="section === 'checks'" @click="openChecks"><ShieldCheck aria-hidden="true" />Checks<template v-if="checks"> ({{ checks.findings.length }})</template></button>
              <button type="button" role="tab" :aria-selected="section === 'live'" @click="openLive"><Activity aria-hidden="true" />Live<template v-if="live?.available"> ({{ live.views.length }})</template></button>
            </div>

            <ul v-if="section === 'routes'" class="um-rows" role="tabpanel" aria-label="Routes">
              <li v-for="route in routes" :key="`${route.source_file}:${route.line}:${route.path}`">
                <button type="button" class="um-row" :disabled="!route.component_id" :aria-current="route.component_id && route.component_id === selectedId ? 'true' : undefined" @click="select(route.component_id)">
                  <span class="um-path">{{ route.path }}</span>
                  <span class="um-sub">
                    <template v-if="route.redirect !== null && !route.component">redirects to {{ route.redirect || '/' }}</template>
                    <template v-else>{{ route.component || route.file || 'component not resolved' }}</template>
                    <template v-if="route.lazy"> · lazy</template><template v-if="route.kind === 'file'"> · file-based</template>
                  </span>
                </button>
              </li>
              <li v-if="!routes.length" class="kb-note">No routes match.</li>
            </ul>

            <ul v-else-if="section === 'components'" class="um-rows" role="tabpanel" aria-label="Components">
              <li v-for="component in components" :key="component.id">
                <button type="button" class="um-row" :aria-current="component.id === selectedId ? 'true' : undefined" @click="select(component.id)">
                  <span class="um-path">{{ component.name }}</span>
                  <span class="um-sub">{{ component.file }}</span>
                </button>
              </li>
              <li v-if="!components.length" class="kb-note">No components match.</li>
            </ul>

            <div v-else-if="section === 'checks'" class="um-rows" role="tabpanel" aria-label="Checks">
              <p v-if="checksLoading" class="kb-note" role="status"><LoaderCircle class="kb-spin" aria-hidden="true" /> Checking the source…</p>
              <p v-else-if="checksError" role="alert" class="kb-rev-note" data-tone="bad"><CircleAlert aria-hidden="true" />{{ checksError }}</p>
              <template v-else-if="checks">
                <button v-for="item in CHECK_AREAS" :key="item.id" type="button" class="um-row" :aria-current="area === item.id ? 'true' : undefined" @click="area = item.id">
                  <span class="um-path">{{ item.label }}</span>
                  <span class="um-sub">{{ checks.by_area[item.id] }} finding{{ checks.by_area[item.id] === 1 ? '' : 's' }}</span>
                </button>
                <p class="kb-note">{{ checks.files_checked }} files checked without running the project. These rules catch common mistakes; they do not replace a linter or a manual accessibility review.</p>
                <p v-for="note in checks.notes" :key="note" class="kb-note">{{ note }}</p>
              </template>
            </div>

            <div v-else-if="section === 'live'" class="um-rows" role="tabpanel" aria-label="Live screen views">
              <p v-if="liveLoading && !live" class="kb-note" role="status"><LoaderCircle class="kb-spin" aria-hidden="true" /> Reading screen views…</p>
              <p v-else-if="live && !live.available" class="kb-rev-note" data-tone="warn"><CircleAlert aria-hidden="true" />{{ live.reason }}</p>
              <template v-else-if="live">
                <button v-for="screen in liveScreens" :key="screen.route" type="button" class="um-row" :aria-current="liveRoute === screen.route ? 'true' : undefined" @click="liveRoute = screen.route; liveDetail = null">
                  <span class="um-path">{{ screen.route }}</span>
                  <span class="um-sub">{{ screen.views }} view{{ screen.views === 1 ? '' : 's' }} · LCP {{ milliseconds(screen.lcp_p75_ms) }}<template v-if="screen.errors"> · {{ screen.errors }} error{{ screen.errors === 1 ? '' : 's' }}</template></span>
                </button>
                <p v-if="!live.views.length" class="kb-note">No screen views in the last {{ live.window_hours }} hours. They appear once the browser snippet is installed in the application and its pages are used.</p>
                <button v-else type="button" class="kb-btn" :disabled="liveLoading" @click="loadLive"><LoaderCircle v-if="liveLoading" class="kb-spin" aria-hidden="true" />Refresh</button>
              </template>
            </div>

            <ul v-else class="um-rows" role="tabpanel" aria-label="Requests">
              <li v-for="call in requests" :key="call.id" class="um-row um-static">
                <span class="um-path"><b class="um-method">{{ call.method }}</b> {{ call.url || call.raw }}<span v-if="!call.url" class="kb-tag" data-tone="warn">built at run time</span></span>
                <span class="um-sub">{{ call.file }}:{{ call.line }}<template v-if="call.symbol"> · {{ call.symbol }}</template></span>
                <span v-if="ownersOf(call.id).length" class="um-chips">
                  <button v-for="owner in ownersOf(call.id)" :key="owner.id" type="button" class="um-chip" @click="select(owner.id); section = 'components'">{{ owner.name }}</button>
                </span>
              </li>
              <li v-if="!requests.length" class="kb-note">No requests match.</li>
            </ul>
          </div>

          <div class="kb-panel um-detail" aria-live="polite">
            <template v-if="section === 'live'">
              <h2 class="kb-panel-title"><Activity aria-hidden="true" />{{ liveRoute || 'Screen views' }}</h2>
              <p v-if="liveComponent" class="kb-project-meta">Built in <button type="button" class="um-chip" @click="select(liveComponent.id); section = 'components'">{{ liveComponent.name }}</button> {{ liveComponent.file }}</p>
              <p class="kb-note">Timings are reported by users' browsers. A view is sampled, so counts are a share of real traffic.</p>
              <table v-if="liveViews.length" class="um-table">
                <thead><tr><th scope="col">Time</th><th scope="col">LCP</th><th scope="col">INP</th><th scope="col">CLS</th><th scope="col">Requests</th><th scope="col">Errors</th></tr></thead>
                <tbody>
                  <tr v-for="view in liveViews" :key="view.trace_id" class="um-view" :aria-current="liveDetail?.view?.trace_id === view.trace_id ? 'true' : undefined" tabindex="0" role="button" @click="openView(view.trace_id)" @keydown.enter="openView(view.trace_id)">
                    <td>{{ time(view.timestamp) }}</td>
                    <td><span class="kb-tag" :data-tone="vitalTone('lcp', view.lcp_ms)">{{ milliseconds(view.lcp_ms) }}</span></td>
                    <td><span class="kb-tag" :data-tone="vitalTone('inp', view.inp_ms)">{{ milliseconds(view.inp_ms) }}</span></td>
                    <td><span class="kb-tag" :data-tone="vitalTone('cls', view.cls)">{{ view.cls ?? '–' }}</span></td>
                    <td>{{ view.requests }}<template v-if="view.failed_requests"> ({{ view.failed_requests }} failed)</template> · {{ milliseconds(view.request_ms) }}</td>
                    <td>{{ view.errors }}</td>
                  </tr>
                </tbody>
              </table>
              <template v-if="liveDetail">
                <p v-if="!liveDetail.available" class="kb-rev-note" data-tone="warn"><CircleAlert aria-hidden="true" />{{ liveDetail.reason }}</p>
                <template v-else>
                  <h3 class="um-heading"><Waypoints aria-hidden="true" />Requests this view sent</h3>
                  <table v-if="liveDetail.requests?.length" class="um-table">
                    <thead><tr><th scope="col">Method</th><th scope="col">Route</th><th scope="col">Status</th><th scope="col">Time</th></tr></thead>
                    <tbody>
                      <tr v-for="request in liveDetail.requests" :key="request.trace_id">
                        <td class="um-method">{{ request.method }}</td><td class="um-path">{{ request.route }}</td>
                        <td><span class="kb-tag" :data-tone="request.failed ? 'bad' : 'ok'">{{ request.status_code ?? '–' }}</span></td>
                        <td>{{ milliseconds(request.duration_ms) }}</td>
                      </tr>
                    </tbody>
                  </table>
                  <p v-else class="kb-note">No recorded request names this view. Requests are recorded only when the back end sampled them.</p>
                  <p v-if="liveDetail.errors?.length" class="kb-note"><b>JavaScript errors:</b> {{ liveDetail.errors.map((item) => item.error_type).join(', ') }}</p>
                  <p v-if="liveDetail.slow_interactions?.length" class="kb-note"><b>Slow interactions:</b> {{ liveDetail.slow_interactions.map((item) => milliseconds(item.duration_ms)).join(', ') }}</p>
                </template>
              </template>
            </template>
            <template v-else-if="section === 'checks'">
              <h2 class="kb-panel-title"><ShieldCheck aria-hidden="true" />{{ CHECK_AREAS.find((item) => item.id === area)?.label }}</h2>
              <p v-if="checks && !checkGroups.length" class="kb-rev-note" data-tone="ok"><CircleCheck aria-hidden="true" />No findings in this area{{ search ? ' match the filter' : '' }}.</p>
              <section v-for="group in checkGroups" :key="group.file" class="um-check-file">
                <h3 class="um-heading">{{ group.file }} <span class="kb-tag">{{ group.findings.length }}</span></h3>
                <ul class="um-plain">
                  <li v-for="finding in group.findings" :key="`${finding.rule}:${finding.line}`"><b class="um-method">{{ finding.line }}</b> {{ finding.message }}</li>
                </ul>
              </section>
            </template>
            <template v-else-if="detail">
              <h2 class="kb-panel-title"><Boxes aria-hidden="true" />{{ detail.component.name }}</h2>
              <p class="kb-project-meta">{{ detail.component.file }}:{{ detail.component.line }}<template v-if="detail.component.selector"> · &lt;{{ detail.component.selector }}&gt;</template></p>

              <h3 class="um-heading"><Route aria-hidden="true" />Shown at</h3>
              <p v-if="detail.routes.length" class="um-chips"><span v-for="route in detail.routes" :key="route.path" class="kb-tag">{{ route.path }}</span></p>
              <p v-else class="kb-note">No route renders this component directly.</p>

              <h3 class="um-heading"><Boxes aria-hidden="true" />Uses</h3>
              <p v-if="detail.uses.length" class="um-chips"><button v-for="child in detail.uses" :key="child.id" type="button" class="um-chip" @click="select(child.id)">{{ child.name }}</button></p>
              <p v-else class="kb-note">No project components.</p>

              <h3 class="um-heading"><Boxes aria-hidden="true" />Used by</h3>
              <p v-if="detail.usedBy.length" class="um-chips"><button v-for="parent in detail.usedBy" :key="parent.id" type="button" class="um-chip" @click="select(parent.id)">{{ parent.name }}</button></p>
              <p v-else class="kb-note">No other component.</p>

              <h3 class="um-heading"><Database aria-hidden="true" />Stores and services</h3>
              <ul v-if="detail.stores.length" class="um-plain">
                <li v-for="store in detail.stores" :key="store!.id"><b>{{ store!.name }}</b> · {{ storeKindLabel(store!.kind) }} · {{ store!.file }}:{{ store!.line }}</li>
              </ul>
              <p v-else class="kb-note">None.</p>

              <h3 class="um-heading"><Waypoints aria-hidden="true" />Requests</h3>
              <table v-if="detail.requests.length" class="um-table">
                <thead><tr><th scope="col">Method</th><th scope="col">Address</th><th scope="col">Made in</th></tr></thead>
                <tbody>
                  <tr v-for="call in detail.requests" :key="call.id">
                    <td class="um-method">{{ call.method }}</td>
                    <td class="um-path">{{ call.url || call.raw }}<span v-if="!call.url" class="kb-tag" data-tone="warn">built at run time</span></td>
                    <td>{{ call.file }}:{{ call.line }}<template v-if="!call.direct"> · through imports</template></td>
                  </tr>
                </tbody>
              </table>
              <p v-else class="kb-note">This component makes no requests itself or through the modules it imports.</p>
            </template>
            <div v-else class="kb-empty um-hint">
              <div class="kb-empty-icon"><LayoutPanelLeft aria-hidden="true" /></div>
              <h3>Select a route or component</h3>
              <p>See which components it uses, which stores it reads, and which requests it makes.</p>
            </div>
          </div>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
.um-wrap { max-width: 90rem; }
.um-top { display: flex; flex-wrap: wrap; align-items: center; gap: 0.625rem 1rem; }
.um-search { position: relative; flex: 1 1 16rem; min-width: 12rem; margin-left: auto; }
.um-search svg { position: absolute; top: 50%; left: 0.625rem; width: 0.9375rem; height: 0.9375rem; transform: translateY(-50%); color: var(--shell-text-3); pointer-events: none; }
.um-search .kb-input { width: 100%; padding-left: 2rem; }
.um-app { flex: 0 1 16rem; }
.um-body { display: grid; grid-template-columns: minmax(0, 28rem) minmax(0, 1fr); gap: 1rem; align-items: start; }
@media (max-width: 64rem) { .um-body { grid-template-columns: minmax(0, 1fr); } }
.um-list { display: flex; flex-direction: column; gap: 0.75rem; }
.um-rows { display: flex; flex-direction: column; gap: 0.25rem; max-height: calc(100vh - 20rem); min-height: 12rem; margin: 0; padding: 0; overflow-y: auto; list-style: none; }
.um-row { display: flex; flex-direction: column; gap: 0.125rem; width: 100%; padding: 0.4375rem 0.625rem; border: 1px solid transparent; border-radius: 0.5rem; background: transparent; color: var(--shell-text); font-size: 0.8125rem; text-align: left; }
button.um-row:not(:disabled) { cursor: pointer; }
button.um-row:not(:disabled):hover, .um-row[aria-current="true"] { border-color: rgb(var(--shell-grad-a-rgb) / 0.5); background: rgb(var(--shell-grad-a-rgb) / 0.08); }
.um-static { border-color: var(--shell-line); }
.um-path { font-family: var(--font-mono); overflow-wrap: anywhere; }
.um-path .kb-tag { margin-left: 0.5rem; }
.um-sub { color: var(--shell-text-3); font-size: 0.75rem; overflow-wrap: anywhere; }
.um-method { font-family: var(--font-mono); font-weight: 700; color: rgb(var(--shell-grad-a-rgb)); }
.um-detail { min-height: 16rem; }
.um-heading { display: flex; align-items: center; gap: 0.375rem; margin: 1rem 0 0.375rem; font-size: 0.8125rem; font-weight: 600; color: var(--shell-text-2); }
.um-heading svg { width: 0.875rem; height: 0.875rem; }
.um-chips { display: flex; flex-wrap: wrap; gap: 0.375rem; margin: 0; }
.um-chip { padding: 0.1875rem 0.5625rem; border: 1px solid var(--shell-line-strong); border-radius: 999px; background: transparent; color: var(--shell-text); font-family: var(--font-mono); font-size: 0.75rem; cursor: pointer; }
.um-chip:hover { border-color: rgb(var(--shell-grad-a-rgb) / 0.6); color: rgb(var(--shell-grad-a-rgb)); }
.um-plain { margin: 0; padding: 0; list-style: none; font-size: 0.8125rem; overflow-wrap: anywhere; }
.um-plain li + li { margin-top: 0.25rem; }
.um-table { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
.um-table th { padding: 0.375rem 0.5rem; color: var(--shell-text-2); font-weight: 600; text-align: left; }
.um-table td { padding: 0.375rem 0.5rem; border-top: 1px solid var(--shell-line); vertical-align: top; overflow-wrap: anywhere; }
.um-hint { border: 0; }
.um-check-file + .um-check-file { margin-top: 0.5rem; }
.um-view { cursor: pointer; }
.um-view:hover td, .um-view[aria-current="true"] td { background: rgb(var(--shell-grad-a-rgb) / 0.08); }
.um-check-file .um-heading { font-family: var(--font-mono); overflow-wrap: anywhere; }
</style>
