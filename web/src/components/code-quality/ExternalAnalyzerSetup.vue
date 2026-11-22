<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { CheckCircle2, Copy, Info, LoaderCircle, Save, ShieldCheck, Trash2, Wrench, X } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';
import { toast, toastMessage } from '../../composables/useToasts';
import ToggleCard from '../ToggleCard.vue';

type Settings = {
  sonarqube: { enabled: boolean; server_url: string; project_key: string; source_upload_consent: boolean; credential_id?: string };
  joern: { enabled: boolean };
  worker: { enabled: boolean; endpoint: string; certificate_sha256: string; source_transfer_consent: boolean };
};
type Capability = { enabled: boolean; status: 'ready' | 'unavailable'; reason?: string | null; scanner_available?: boolean; analyzer_available?: boolean; version?: string };
const props = defineProps<{ projectId: string }>();
const blank = (): Settings => ({ sonarqube: { enabled: false, server_url: '', project_key: '', source_upload_consent: false }, joern: { enabled: false }, worker: { enabled: false, endpoint: '', certificate_sha256: '', source_transfer_consent: false } });
const settings = ref<Settings>(blank());
const capabilities = ref<{ deployment_mode?: string; worker?: Capability; sonarqube?: Capability; joern?: Capability }>({});
const deploymentMode = ref<'developer' | 'production'>('developer');
const developerMode = ref({ enabled: false, locked: true, can_manage: false, reason: '' });
const developerModeLoaded = ref(false);
const developerModeSaving = ref(false);
const workerCheck = ref<'idle' | 'checking' | 'ready' | 'failed'>('idle');
const sonarCheck = ref<'idle' | 'checking' | 'ready' | 'failed'>('idle');
const verifiedWorkerTarget = ref('');
const workerIdentity = () => `${settings.value.worker.endpoint.trim()}|${settings.value.worker.certificate_sha256.trim().toLowerCase()}`;
const setupGuideOpen = ref(false);
const copiedGuideItem = ref('');
const copyError = toastMessage('error');
const workerFingerprintLoading = ref(false);
const workerFingerprintMessage = toastMessage('success');
const workerFingerprintError = toastMessage('error');
const token = ref('');
const tokenSaved = ref(false);
const loading = ref(false);
const saving = ref(false);
const error = toastMessage('error');
const message = toastMessage('success');
const workerReady = computed(() => deploymentMode.value === 'production'
  ? capabilities.value.worker?.status === 'ready'
  : (developerMode.value.enabled && settings.value.worker.enabled && settings.value.worker.source_transfer_consent
    && !!settings.value.worker.endpoint.trim() && /^[a-f0-9]{64}$/i.test(settings.value.worker.certificate_sha256.trim())
    && workerCheck.value === 'ready'));
const canEnableSonar = computed(() => !!settings.value.sonarqube.server_url.trim()
  && !!settings.value.sonarqube.project_key.trim() && settings.value.sonarqube.source_upload_consent && workerReady.value);
const canCheckSonar = computed(() => tokenSaved.value
  && !token.value.trim()
  && !!settings.value.sonarqube.server_url.trim()
  && !!settings.value.sonarqube.project_key.trim()
  && (deploymentMode.value === 'production'
    ? capabilities.value.worker?.status === 'ready'
    : developerMode.value.enabled && !!settings.value.worker.endpoint.trim()
      && /^[a-f0-9]{64}$/i.test(settings.value.worker.certificate_sha256.trim())));
const certificateCommand = 'api/analyzer-worker/provision-certs.sh /secure/path/analyzer-certs WORKER_DNS_NAME WORKER_IP_ADDRESS';
const apiWorkerConfiguration = 'ANALYZER_API_TLS_DIR=/secure/path/analyzer-certs/api';
const remoteWorkerConfiguration = `SONAR_DB_PASSWORD=REPLACE_WITH_LONG_RANDOM_PASSWORD
SONARQUBE_HOST_PORT=19000
ANALYZER_WORKER_BIND=WORKER_IP_ADDRESS
ANALYZER_WORKER_PORT=9443
ANALYZER_CERT_GID=REMOTE_WORKER_GROUP_ID`;
const remoteWorkerImageBuildCommand = 'cd api && docker build -f analyzer-worker/Dockerfile -t devassistant/analyzer-worker:joern-4.0.664-scanner-8.1.0.6389 .';
const remoteWorkerImageExportCommand = 'docker save -o /tmp/da-analyzer-worker.tar devassistant/analyzer-worker:joern-4.0.664-scanner-8.1.0.6389';
const remoteSonarCopyCommand = 'scp /tmp/da-analyzer-worker.tar api/analyzer-worker/sonarqube-only.compose.yaml api/analyzer-worker/remote-worker.compose.yaml USER@ANALYZER_HOST:~/developer-assistant-analyzers/';
const remoteWorkerTlsCopyCommand = 'scp /secure/path/analyzer-certs/worker/* USER@ANALYZER_HOST:~/developer-assistant-analyzers/worker-tls/';
const remoteSonarStartCommand = 'cd ~/developer-assistant-analyzers && docker load -i da-analyzer-worker.tar && docker compose -f sonarqube-only.compose.yaml -f remote-worker.compose.yaml up -d';
const copyGuideItem = async (key: string, value: string) => {
  copyError.value = '';
  try {
    await navigator.clipboard.writeText(value);
    copiedGuideItem.value = key;
    window.setTimeout(() => { if (copiedGuideItem.value === key) copiedGuideItem.value = ''; }, 1800);
  } catch { copyError.value = 'Clipboard access is unavailable. Select and copy the command manually.'; }
};
const fetchWorkerCertificateFingerprint = async () => {
  workerFingerprintLoading.value = true;
  workerFingerprintMessage.value = '';
  workerFingerprintError.value = '';
  try {
    const data = await request(basePath() + '/worker/fingerprint', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint: settings.value.worker.endpoint }),
    });
    if (typeof data.certificate_sha256 !== 'string' || !/^[a-f0-9]{64}$/i.test(data.certificate_sha256)) {
      throw new Error('The worker returned an invalid certificate fingerprint.');
    }
    settings.value.worker.certificate_sha256 = data.certificate_sha256.toLowerCase();
    workerFingerprintMessage.value = 'Fingerprint fetched from the CA-verified worker connection. No source was transferred.';
  } catch (cause: any) {
    workerFingerprintError.value = cause?.message || 'The worker certificate fingerprint could not be fetched.';
  } finally {
    workerFingerprintLoading.value = false;
  }
};
const setWorkerEnabled = (enabled: boolean) => {
  settings.value.worker.enabled = enabled;
  if (!enabled) {
    settings.value.sonarqube.enabled = false;
    settings.value.joern.enabled = false;
  }
};
const request = async (path: string, init?: RequestInit) => {
  const response = await makeRequest(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || 'External analyzer settings request failed.');
  return data;
};
const basePath = () => `/api/code-quality/projects/${encodeURIComponent(props.projectId)}/external-analyzers`;
const load = async () => {
  error.value = ''; message.value = '';
  loading.value = true;
  try {
    const modeData = await request('/api/code-quality/developer-mode');
    developerMode.value = {
      enabled: modeData.enabled === true,
      locked: modeData.locked === true,
      can_manage: modeData.can_manage === true,
      reason: typeof modeData.reason === 'string' ? modeData.reason : '',
    };
    developerModeLoaded.value = true;
    deploymentMode.value = modeData.deployment_mode === 'production' ? 'production' : 'developer';
    if (!props.projectId) {
      settings.value = blank(); capabilities.value = {}; tokenSaved.value = false; verifiedWorkerTarget.value = '';
      return;
    }
    const data = await request(basePath());
    settings.value = { ...blank(), ...(data.settings || {}), sonarqube: { ...blank().sonarqube, ...(data.settings?.sonarqube || {}) }, joern: { ...blank().joern, ...(data.settings?.joern || {}) } };
    capabilities.value = data.capabilities || {};
    deploymentMode.value = data.deployment_mode === 'production' ? 'production' : 'developer';
    workerCheck.value = capabilities.value.worker?.status === 'ready' ? 'ready' : 'idle';
    verifiedWorkerTarget.value = workerCheck.value === 'ready'
      ? workerIdentity() : '';
    tokenSaved.value = data.sonarqube_credential?.saved === true;
  } catch (cause: any) { error.value = cause?.message || 'External analyzer settings could not be loaded.'; }
  finally { loading.value = false; }
};
const updateDeveloperMode = async (enabled: boolean) => {
  developerModeSaving.value = true; error.value = ''; message.value = '';
  try {
    const data = await request('/api/code-quality/developer-mode', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }),
    });
    developerMode.value = {
      enabled: data.enabled === true,
      locked: data.locked === true,
      can_manage: data.can_manage === true,
      reason: typeof data.reason === 'string' ? data.reason : '',
    };
    if (props.projectId && !enabled) {
      const reason = 'Developer Mode remote analyzers are disabled by the global administrator.';
      capabilities.value = {
        ...capabilities.value,
        worker: { enabled: false, status: 'unavailable', reason },
        sonarqube: { enabled: settings.value.sonarqube.enabled, status: 'unavailable', reason },
        joern: { enabled: settings.value.joern.enabled, status: 'unavailable', reason },
      };
    } else if (props.projectId && enabled) {
      try {
        const projectData = await request(basePath());
        capabilities.value = projectData.capabilities || {};
        workerCheck.value = capabilities.value.worker?.status === 'ready' ? 'ready' : 'idle';
        verifiedWorkerTarget.value = workerCheck.value === 'ready' ? workerIdentity() : '';
      } catch {
        capabilities.value = {
          ...capabilities.value,
          worker: { enabled: false, status: 'unavailable', reason: 'Worker status could not be refreshed. Test the connection and retry.' },
        };
        workerCheck.value = 'idle';
        verifiedWorkerTarget.value = '';
      }
    }
    message.value = enabled
      ? 'Developer Mode remote analyzers are enabled for all projects.'
      : 'Developer Mode remote analyzers are disabled for all projects. Project settings were retained.';
  } catch (cause: any) { error.value = cause?.message || 'Developer Mode could not be changed.'; }
  finally { developerModeSaving.value = false; }
};
const save = async () => {
  saving.value = true; error.value = ''; message.value = '';
  try {
    if (developerMode.value.enabled && settings.value.sonarqube.enabled && !canEnableSonar.value) throw new Error('Configure the server, project key, and source upload consent before enabling SonarQube.');
    if (deploymentMode.value === 'developer' && developerMode.value.enabled
        && (settings.value.sonarqube.enabled || settings.value.joern.enabled) && !workerReady.value) throw new Error('Configure and verify the analyzer worker, then consent to source transfer before enabling analyzers.');
    const data = await request(basePath(), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings.value) });
    settings.value = data.settings;
    capabilities.value = data.capabilities || {};
    if (deploymentMode.value === 'developer' && settings.value.worker.enabled
      && capabilities.value.worker?.status === 'ready') {
      workerCheck.value = 'ready';
      verifiedWorkerTarget.value = workerIdentity();
    }
    tokenSaved.value = data.sonarqube_credential?.saved === true;
    message.value = 'External analyzer settings saved for this project.';
  } catch (cause: any) { error.value = cause?.message || 'External analyzer settings could not be saved.'; }
  finally { saving.value = false; }
};
const checkWorker = async () => {
  workerCheck.value = 'checking'; error.value = ''; message.value = '';
  try {
    const data = await request(basePath() + '/worker/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ worker: settings.value.worker }) });
    workerCheck.value = 'ready'; capabilities.value = { ...capabilities.value, worker: { enabled: true, status: 'ready' }, ...data.analyzers };
    verifiedWorkerTarget.value = workerIdentity();
    message.value = 'The analyzer worker certificate and connection were verified.';
  } catch (cause: any) { workerCheck.value = 'failed'; error.value = cause?.message || 'The analyzer worker could not be verified.'; }
};
const checkSonarAccess = async () => {
  sonarCheck.value = 'checking'; error.value = ''; message.value = '';
  try {
    const data = await request(basePath() + '/sonarqube/check', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        server_url: settings.value.sonarqube.server_url,
        project_key: settings.value.sonarqube.project_key,
        worker: settings.value.worker,
      }),
    });
    sonarCheck.value = 'ready';
    const version = typeof data.server_version === 'string' && data.server_version !== 'unknown'
      ? ` (SonarQube ${data.server_version})` : '';
    toast.success(`Project result access verified${version}. No source was uploaded; analysis-write permission was not tested.`);
  } catch (cause: any) {
    sonarCheck.value = 'failed';
    toast.error(cause?.message || 'SonarQube project access could not be verified.');
  }
};
const saveToken = async () => {
  if (!token.value.trim()) return;
  saving.value = true; error.value = ''; message.value = '';
  try {
    if (!settings.value.sonarqube.credential_id) await save();
    await request(basePath() + '/credential', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: token.value }) });
    token.value = ''; tokenSaved.value = true; sonarCheck.value = 'idle'; message.value = 'SonarQube token saved in the host OS credential store.';
  } catch (cause: any) { error.value = cause?.message || 'SonarQube token could not be saved.'; }
  finally { saving.value = false; }
};
const removeToken = async () => {
  saving.value = true; error.value = ''; message.value = '';
  try { await request(basePath() + '/credential', { method: 'DELETE' }); tokenSaved.value = false; sonarCheck.value = 'idle'; message.value = 'Saved SonarQube token removed from the OS credential store.'; }
  catch (cause: any) { error.value = cause?.message || 'SonarQube token could not be removed.'; }
  finally { saving.value = false; }
};
watch(() => props.projectId, () => { token.value = ''; workerCheck.value = 'idle'; verifiedWorkerTarget.value = ''; void load(); }, { immediate: true });
watch(() => [settings.value.worker.endpoint, settings.value.worker.certificate_sha256], () => {
  const current = workerIdentity();
  if (current !== verifiedWorkerTarget.value) workerCheck.value = 'idle';
  sonarCheck.value = 'idle';
});
watch(() => [settings.value.sonarqube.server_url, settings.value.sonarqube.project_key], () => {
  sonarCheck.value = 'idle';
});
</script>

<template>
  <section class="space-y-5" aria-label="External static analyzers">
    <div class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6">
      <h2 class="text-xl font-semibold">External static analyzers</h2>
      <p class="mt-1 max-w-3xl text-sm leading-relaxed text-slate-400">Optional SonarQube and Joern analyzers add revision-scoped evidence to assessment. They do not calculate scores or prove the absence of defects.</p>
      <div v-if="developerModeLoaded" class="mt-5 rounded-xl border border-slate-800 bg-slate-950/50 p-4 sm:p-5">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="min-w-0"><h3 class="font-semibold text-slate-100">Global Developer Mode</h3><p class="mt-1 max-w-3xl text-sm leading-relaxed text-slate-400">Controls remote analyzer workers for every project. Turning it off stops worker checks and new remote analysis jobs while keeping project settings.</p></div>
          <ToggleCard :model-value="developerMode.enabled" :disabled="developerModeSaving || developerMode.locked || !developerMode.can_manage" :label="deploymentMode === 'production' ? 'Developer Mode (locked)' : 'Enable Developer Mode'" :description="deploymentMode === 'production' ? 'Production uses its deployment-installed local worker' : 'Global remote analyzer access'" @update:model-value="updateDeveloperMode" />
        </div>
        <p v-if="developerMode.locked" role="status" class="mt-3 text-xs text-indigo-200">{{ developerMode.reason || 'Production Mode locks Developer Mode off.' }}</p>
        <p v-else-if="!developerMode.can_manage" class="mt-3 text-xs text-slate-400">Only the global administrator can change this setting.</p>
        <p v-else-if="!developerMode.enabled" class="mt-3 text-xs text-amber-200">Remote analyzer worker access is globally suspended. Re-enable Developer Mode to resume configured project workers.</p>
      </div>
      <div v-if="!projectId" class="mt-5 rounded-lg border border-slate-800 p-3 text-sm text-slate-400">Select a project revision to configure analyzers.</div>
      <div v-else-if="loading" class="mt-5 flex items-center gap-2 text-sm text-slate-400"><LoaderCircle class="motion-safe:animate-spin" :size="16" />Loading analyzer settings…</div>
      <div v-else class="mt-5 space-y-5">
        <div v-if="deploymentMode === 'developer'" class="rounded-xl border border-amber-400/30 bg-amber-400/5 p-4 sm:p-5">
          <div class="flex items-start gap-3">
            <ShieldCheck class="mt-0.5 shrink-0 text-amber-200" :size="18" />
            <div class="min-w-0 flex-1">
              <h3 class="font-semibold text-amber-100">Developer Mode analyzer worker</h3>
              <p class="mt-1 text-sm leading-relaxed text-amber-100/80">The analyzer worker runs at its configured local or remote endpoint. When enabled, the API sends only the selected revision after project consent.</p>
              <details class="mt-3 rounded-lg border border-amber-200/15 bg-slate-950/40 p-3 text-sm text-slate-200">
                <summary class="flex cursor-pointer list-none items-center gap-2 font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300"><Info :size="15" />Security details</summary>
                <p class="mt-2 leading-relaxed text-slate-300">The remote host runs SonarQube, PostgreSQL, and the isolated analyzer-worker container; it does not run the Developer Assistant application or require its repository checkout. The worker receives only the selected immutable revision over mutual TLS and removes temporary source artifacts after each job. SonarQube requires separate project consent. Its token stays in the OS credential store and is used for that job only.</p>
              </details>
              <button type="button" class="mt-3 inline-flex min-h-10 items-center gap-2 rounded-lg border border-amber-200/25 px-3 text-sm font-medium text-amber-100 hover:bg-amber-200/10 focus-visible:outline focus-visible:outline-2 focus-visible:outline-amber-200" :aria-expanded="setupGuideOpen" aria-controls="remote-worker-setup-guide" @click="setupGuideOpen = !setupGuideOpen"><Wrench :size="15" />{{ setupGuideOpen ? 'Close setup guide' : 'Set up remote analyzers' }}</button>
            </div>
          </div>
          <section v-if="setupGuideOpen" id="remote-worker-setup-guide" role="region" aria-label="Remote SonarQube and analyzer-worker setup steps" class="mt-4 space-y-4 rounded-xl border border-amber-200/20 bg-slate-950/65 p-4 text-sm text-slate-200 sm:p-5">
            <div class="flex items-start justify-between gap-3"><div><h3 class="font-semibold text-amber-100">Remote SonarQube and analyzer worker</h3><p class="mt-1 text-xs leading-relaxed text-slate-400">Transfer the pinned worker image, Compose files, and worker certificate bundle only. Do not check out or copy the Developer Assistant repository.</p></div><button type="button" class="rounded-md p-1 text-slate-400 hover:text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300" aria-label="Close remote analyzer setup guide" @click="setupGuideOpen = false"><X :size="16" /></button></div>
            <ol class="space-y-4">
              <li>
                <h4 class="font-medium">1. Prepare remote analyzers</h4>
                <p class="mt-1 text-xs leading-relaxed text-slate-400">From the repository root, build and export the pinned worker image. Create a private <code>~/developer-assistant-analyzers/worker-tls/</code> directory remotely, then transfer the image archive, both Compose files, and only the worker TLS files.</p>
                <div class="mt-2 flex items-start gap-2"><code class="min-w-0 flex-1 overflow-x-auto rounded-lg bg-black/35 p-3 font-mono text-xs text-slate-200">{{ remoteWorkerImageBuildCommand }}</code><button type="button" class="inline-flex shrink-0 items-center gap-1 rounded-md border border-slate-700 px-2 py-2 text-xs hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300" :aria-label="copiedGuideItem === 'worker-build' ? 'Worker image build command copied' : 'Copy worker image build command'" @click="copyGuideItem('worker-build', remoteWorkerImageBuildCommand)"><CheckCircle2 v-if="copiedGuideItem === 'worker-build'" :size="14" /><Copy v-else :size="14" /><span>{{ copiedGuideItem === 'worker-build' ? 'Copied' : 'Copy' }}</span></button></div>
                <div class="mt-2 flex items-start gap-2"><code class="min-w-0 flex-1 overflow-x-auto rounded-lg bg-black/35 p-3 font-mono text-xs text-slate-200">{{ remoteWorkerImageExportCommand }}</code><button type="button" class="inline-flex shrink-0 items-center gap-1 rounded-md border border-slate-700 px-2 py-2 text-xs hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300" :aria-label="copiedGuideItem === 'worker-export' ? 'Worker image export command copied' : 'Copy worker image export command'" @click="copyGuideItem('worker-export', remoteWorkerImageExportCommand)"><CheckCircle2 v-if="copiedGuideItem === 'worker-export'" :size="14" /><Copy v-else :size="14" /><span>{{ copiedGuideItem === 'worker-export' ? 'Copied' : 'Copy' }}</span></button></div>
                <div class="mt-2 flex items-start gap-2"><code class="min-w-0 flex-1 overflow-x-auto rounded-lg bg-black/35 p-3 font-mono text-xs text-slate-200">{{ remoteSonarCopyCommand }}</code><button type="button" class="inline-flex shrink-0 items-center gap-1 rounded-md border border-slate-700 px-2 py-2 text-xs hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300" :aria-label="copiedGuideItem === 'sonar-copy' ? 'Compose file copy command copied' : 'Copy Compose file copy command'" @click="copyGuideItem('sonar-copy', remoteSonarCopyCommand)"><CheckCircle2 v-if="copiedGuideItem === 'sonar-copy'" :size="14" /><Copy v-else :size="14" /><span>{{ copiedGuideItem === 'sonar-copy' ? 'Copied' : 'Copy' }}</span></button></div>
                <div class="mt-2 flex items-start gap-2"><code class="min-w-0 flex-1 overflow-x-auto rounded-lg bg-black/35 p-3 font-mono text-xs text-slate-200">{{ remoteWorkerTlsCopyCommand }}</code><button type="button" class="inline-flex shrink-0 items-center gap-1 rounded-md border border-slate-700 px-2 py-2 text-xs hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300" :aria-label="copiedGuideItem === 'tls-copy' ? 'Worker certificate copy command copied' : 'Copy worker certificate copy command'" @click="copyGuideItem('tls-copy', remoteWorkerTlsCopyCommand)"><CheckCircle2 v-if="copiedGuideItem === 'tls-copy'" :size="14" /><Copy v-else :size="14" /><span>{{ copiedGuideItem === 'tls-copy' ? 'Copied' : 'Copy' }}</span></button></div>
                <div class="mt-2 flex items-start gap-2"><pre class="min-w-0 flex-1 overflow-x-auto rounded-lg bg-black/35 p-3 font-mono text-xs text-slate-200">{{ remoteWorkerConfiguration }}</pre><button type="button" class="inline-flex shrink-0 items-center gap-1 rounded-md border border-slate-700 px-2 py-2 text-xs hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300" :aria-label="copiedGuideItem === 'sonar-env' ? 'Remote analyzer configuration copied' : 'Copy remote analyzer configuration'" @click="copyGuideItem('sonar-env', remoteWorkerConfiguration)"><CheckCircle2 v-if="copiedGuideItem === 'sonar-env'" :size="14" /><Copy v-else :size="14" /><span>{{ copiedGuideItem === 'sonar-env' ? 'Copied' : 'Copy' }}</span></button></div>
                <p class="mt-1 text-xs leading-relaxed text-slate-400">Set a private <code>.env</code> with the remote worker IP, SonarQube host port, certificate group ID, and a random database password. Then run <code>{{ remoteSonarStartCommand }}</code>. SonarQube remains loopback-only; the worker reaches it at <code>http://sonarqube:9000</code>.</p>
              </li>
              <li>
                <h4 class="font-medium">2. Provision mutual-TLS certificates</h4>
                <p class="mt-1 text-xs leading-relaxed text-slate-400">Run from the Developer Assistant repository root. Replace the placeholders with the remote worker's DNS name and IP. Keep the offline CA keys private and copy only the API bundle here and worker bundle to the remote host.</p>
                <div class="mt-2 flex items-start gap-2"><code class="min-w-0 flex-1 overflow-x-auto rounded-lg bg-black/35 p-3 font-mono text-xs text-slate-200">{{ certificateCommand }}</code><button type="button" class="inline-flex shrink-0 items-center gap-1 rounded-md border border-slate-700 px-2 py-2 text-xs hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300" :aria-label="copiedGuideItem === 'cert' ? 'Certificate command copied' : 'Copy certificate command'" @click="copyGuideItem('cert', certificateCommand)"><CheckCircle2 v-if="copiedGuideItem === 'cert'" :size="14" /><Copy v-else :size="14" /><span>{{ copiedGuideItem === 'cert' ? 'Copied' : 'Copy' }}</span></button></div>
                <p class="mt-1 text-xs leading-relaxed text-slate-400">Point the API at its client bundle. Set the remote worker's key group in the remote <code>.env</code>.</p>
                <div class="mt-2 flex items-start gap-2"><pre class="min-w-0 flex-1 overflow-x-auto rounded-lg bg-black/35 p-3 font-mono text-xs text-slate-200">{{ apiWorkerConfiguration }}</pre><button type="button" class="inline-flex shrink-0 items-center gap-1 rounded-md border border-slate-700 px-2 py-2 text-xs hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300" :aria-label="copiedGuideItem === 'api-env' ? 'API certificate configuration copied' : 'Copy API certificate configuration'" @click="copyGuideItem('api-env', apiWorkerConfiguration)"><CheckCircle2 v-if="copiedGuideItem === 'api-env'" :size="14" /><Copy v-else :size="14" /><span>{{ copiedGuideItem === 'api-env' ? 'Copied' : 'Copy' }}</span></button></div>
                <div class="mt-2 flex items-start gap-2"><pre class="min-w-0 flex-1 overflow-x-auto rounded-lg bg-black/35 p-3 font-mono text-xs text-slate-200">{{ remoteWorkerConfiguration }}</pre><button type="button" class="inline-flex shrink-0 items-center gap-1 rounded-md border border-slate-700 px-2 py-2 text-xs hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300" :aria-label="copiedGuideItem === 'remote-env' ? 'Remote worker configuration copied' : 'Copy remote worker configuration'" @click="copyGuideItem('remote-env', remoteWorkerConfiguration)"><CheckCircle2 v-if="copiedGuideItem === 'remote-env'" :size="14" /><Copy v-else :size="14" /><span>{{ copiedGuideItem === 'remote-env' ? 'Copied' : 'Copy' }}</span></button></div>
              </li>
              <li>
                <h4 class="font-medium">3. Connect this project</h4>
                <p class="mt-1 text-xs leading-relaxed text-slate-400">Set Worker HTTPS address to <code>https://WORKER_IP_ADDRESS:9443</code>, fetch its verified fingerprint, test, and consent to source transfer. Set SonarQube URL to <code>http://sonarqube:9000</code>, add the project key and OS-stored token, then separately consent to SonarQube source upload.</p>
                <p class="mt-1 text-xs text-slate-400">Only the selected revision is sent to the worker. The worker runs Joern and SonarScanner and removes temporary source after the job.</p>
              </li>
            </ol>
          </section>
          <div class="mt-4 grid gap-4 md:grid-cols-2">
            <label class="text-sm font-medium text-slate-300">Worker HTTPS address<input v-model="settings.worker.endpoint" type="url" maxlength="2048" placeholder="https://WORKER_IP_ADDRESS:9443" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 text-sm outline-none focus:border-indigo-400" /></label>
            <div class="text-sm font-medium text-slate-300">
              <label for="worker-certificate-fingerprint">Worker certificate SHA-256 fingerprint</label>
              <input id="worker-certificate-fingerprint" v-model="settings.worker.certificate_sha256" type="text" maxlength="64" spellcheck="false" placeholder="64 hexadecimal characters" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 font-mono text-sm outline-none focus:border-indigo-400" />
              <div class="mt-2 flex flex-wrap items-center gap-3">
                <button type="button" :disabled="workerFingerprintLoading || !developerMode.enabled || !settings.worker.endpoint.trim()" class="inline-flex min-h-9 items-center gap-2 rounded-lg border border-slate-700 bg-slate-900/50 px-3 text-xs font-medium text-slate-200 transition-colors hover:bg-slate-800/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400/70 disabled:cursor-wait disabled:opacity-60" @click="fetchWorkerCertificateFingerprint">
                  <LoaderCircle v-if="workerFingerprintLoading" class="motion-safe:animate-spin" :size="14" />
                  <span>{{ workerFingerprintLoading ? 'Checking worker certificate…' : 'Fetch fingerprint from worker' }}</span>
                </button>
                <span class="text-xs font-normal text-slate-400">Verifies the worker’s CA and hostname before reading its fingerprint; source is not sent.</span>
              </div>
            </div>
            <div class="md:col-span-2"><ToggleCard :model-value="settings.worker.source_transfer_consent" label="Consent to source transfer to the configured analyzer worker" description="Selected source files are sent only after mutual-TLS identity verification. SonarQube upload requires separate consent." @update:model-value="settings.worker.source_transfer_consent = $event" /></div>
          </div>
          <div class="mt-3 flex flex-wrap items-center gap-3">
            <ToggleCard :model-value="settings.worker.enabled" :disabled="!developerMode.enabled" label="Enable analyzer worker" description="This project only; selected source is sent after consent" @update:model-value="setWorkerEnabled" />
            <button type="button" :disabled="!developerMode.enabled || workerCheck === 'checking' || !settings.worker.endpoint.trim() || !/^[a-f0-9]{64}$/i.test(settings.worker.certificate_sha256.trim())" class="inline-flex min-h-[56px] min-w-64 items-center gap-3 rounded-xl border border-slate-700 bg-slate-900/30 px-3 py-3 text-left text-slate-200 transition-colors hover:bg-slate-800/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/70 disabled:cursor-not-allowed disabled:opacity-50" @click="checkWorker"><LoaderCircle v-if="workerCheck === 'checking'" class="motion-safe:animate-spin" :size="16" /><ShieldCheck v-else :size="16" /><span><span class="block text-xs font-semibold">{{ workerCheck === 'checking' ? 'Verifying connection…' : 'Test worker connection' }}</span><span class="mt-0.5 block text-[10px] text-slate-500">Verify address and certificate identity</span></span></button>
            <span v-if="workerCheck === 'ready'" role="status" class="text-sm text-emerald-200">Worker verified</span>
          </div>
          <p class="mt-2 text-xs leading-relaxed text-slate-400">Testing checks the worker connection without sending source. After it succeeds, consent to source transfer and enable the worker for this project.</p>
          <p class="mt-2 text-xs leading-relaxed text-slate-400">This project setting controls access to its configured analyzer worker while the application is in Developer Mode; it does not change the application’s deployment mode. Turn it off and save to stop sending source. SonarQube and Joern run toggles turn off automatically; their configuration and saved token remain available for later.</p>
        </div>
        <div v-else class="rounded-xl border border-indigo-400/25 bg-indigo-400/5 p-4 text-sm text-indigo-100">
          <div class="flex items-start gap-3"><ShieldCheck class="mt-0.5 shrink-0" :size="17" /><div><h3 class="font-semibold">Production Mode local worker</h3><p class="mt-1 leading-relaxed">Analyzer services run on the production application host. Remote worker targets and off-host source transfer are disabled.</p><p v-if="capabilities.worker?.reason" class="mt-2 text-xs text-indigo-100/75">{{ capabilities.worker.reason }}</p><p v-else class="mt-2 text-xs text-emerald-200">Local worker {{ capabilities.worker?.status === 'ready' ? 'available' : 'status unknown' }}</p></div></div>
        </div>
        <div class="rounded-xl border border-slate-800 bg-slate-950/50 p-4 sm:p-5">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div><h3 class="font-semibold">SonarQube</h3><p class="mt-1 text-sm text-slate-400">Runs the installed SonarScanner against the selected revision and sends source only to this server.</p></div>
            <span class="rounded-full px-3 py-1 text-xs" :class="capabilities.sonarqube?.status === 'ready' ? 'bg-emerald-500/10 text-emerald-200' : 'bg-amber-500/10 text-amber-200'">{{ capabilities.sonarqube?.status === 'ready' ? 'Scanner available' : 'Scanner unavailable' }}</span>
          </div>
          <div class="mt-4 grid gap-4 md:grid-cols-2">
            <label class="text-sm font-medium text-slate-300">SonarQube server URL<input v-model="settings.sonarqube.server_url" type="url" maxlength="2048" placeholder="https://sonarqube.example.com" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 text-sm outline-none focus:border-indigo-400" /></label>
            <label class="text-sm font-medium text-slate-300">Project key<input v-model="settings.sonarqube.project_key" maxlength="200" placeholder="my-project" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 text-sm outline-none focus:border-indigo-400" /></label>
            <div class="md:col-span-2"><ToggleCard :model-value="settings.sonarqube.source_upload_consent" label="Consent to source upload to SonarQube" description="Only included business source files from this revision are sent. Enable only for a SonarQube server you trust." @update:model-value="settings.sonarqube.source_upload_consent = $event" /></div>
          </div>
          <label class="mt-4 block text-sm font-medium text-slate-300">SonarQube token
            <input v-model="token" type="password" autocomplete="new-password" :placeholder="tokenSaved ? 'Saved in the OS credential store' : 'Paste a token with project analysis and issue read access'" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 text-sm outline-none focus:border-indigo-400" />
            <span class="mt-1 block text-xs font-normal text-slate-500">Stored in the host OS credential store. The token is used only for the configured SonarQube server.</span>
          </label>
          <p v-if="capabilities.sonarqube?.reason" class="mt-3 text-xs text-amber-200">{{ capabilities.sonarqube.reason }}</p>
          <div class="mt-3 flex flex-wrap items-center gap-2"><button type="button" :disabled="saving || !token.trim()" class="min-h-9 rounded-lg border border-slate-700 px-3 text-sm text-slate-200 disabled:opacity-50" @click="saveToken">Save token</button><button v-if="tokenSaved" type="button" :disabled="saving" class="inline-flex min-h-9 items-center gap-2 rounded-lg border border-rose-500/30 px-3 text-sm text-rose-200 disabled:opacity-50" @click="removeToken"><Trash2 :size="14" />Remove saved token</button><button type="button" :disabled="saving || sonarCheck === 'checking' || !canCheckSonar" class="inline-flex min-h-9 items-center gap-2 rounded-lg border border-indigo-400/30 px-3 text-sm text-indigo-100 hover:bg-indigo-400/10 disabled:cursor-not-allowed disabled:opacity-50" @click="checkSonarAccess"><LoaderCircle v-if="sonarCheck === 'checking'" class="motion-safe:animate-spin" :size="14" /><ShieldCheck v-else :size="14" />{{ sonarCheck === 'checking' ? 'Checking access…' : 'Test SonarQube access' }}</button></div>
          <p v-if="tokenSaved && sonarCheck === 'idle'" class="mt-2 text-xs text-slate-500">Checks access to existing project results with the saved token. Does not upload source, run an assessment, or use the configured AI model.</p>
          <div class="mt-4"><ToggleCard :model-value="settings.sonarqube.enabled" :disabled="!canEnableSonar || (deploymentMode === 'developer' && !developerMode.enabled)" label="Enable SonarQube during assessment" description="Run the configured SonarScanner for this project" @update:model-value="settings.sonarqube.enabled = $event" /></div>
        </div>
        <div class="rounded-xl border border-slate-800 bg-slate-950/50 p-4 sm:p-5">
          <div class="flex flex-wrap items-start justify-between gap-3"><div><h3 class="font-semibold">Joern</h3><p class="mt-1 text-sm text-slate-400">Builds a bounded code property graph for languages supported by the installed Joern frontends.</p></div><span class="rounded-full px-3 py-1 text-xs" :class="capabilities.joern?.status === 'ready' ? 'bg-emerald-500/10 text-emerald-200' : 'bg-amber-500/10 text-amber-200'">{{ capabilities.joern?.status === 'ready' ? 'Joern available' : 'Joern unavailable' }}</span></div>
          <p v-if="capabilities.joern?.reason" class="mt-3 text-xs text-amber-200">{{ capabilities.joern.reason }}</p>
          <div class="mt-4"><ToggleCard :model-value="settings.joern.enabled" :disabled="!workerReady || (deploymentMode === 'developer' && !developerMode.enabled)" label="Enable Joern during assessment" description="Build a bounded code property graph when the configured worker is ready" @update:model-value="settings.joern.enabled = $event" /></div>
        </div>
        <div class="flex justify-end"><button type="button" :disabled="saving" class="inline-flex min-h-10 items-center gap-2 rounded-lg bg-indigo-500 px-4 text-sm font-semibold text-white hover:bg-indigo-400 disabled:opacity-50" @click="save"><LoaderCircle v-if="saving" class="motion-safe:animate-spin" :size="15" /><Save v-else :size="15" />{{ saving ? 'Saving…' : 'Save analyzer settings' }}</button></div>
      </div>
      <p class="mt-5 flex items-start gap-2 border-t border-slate-800 pt-4 text-xs leading-relaxed text-slate-500"><ShieldCheck :size="14" class="mt-0.5 shrink-0" />Integrations are optional. Unsupported languages, unavailable tools, partial scans, and execution limits must remain visible in each report.</p>
    </div>
  </section>
</template>
