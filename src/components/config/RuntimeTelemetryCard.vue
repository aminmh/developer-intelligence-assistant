<script setup lang="ts">
import { Activity, CheckCircle2, RefreshCw, Save, Server, ShieldCheck } from 'lucide-vue-next';
import { onMounted, ref, watch } from 'vue';
import ToggleCard from '../ToggleCard.vue';
import { makeRequest } from '../../ui/makeRequest';
import type { RuntimeStorageTarget, RuntimeTelemetrySettings } from '../../types';

const settings = ref<RuntimeTelemetrySettings | null>(null);
const loading = ref(false);
const saving = ref(false);
const error = ref('');
const savedMessage = ref('');
const p95WindowHours = ref(24);
const refreshMinutes = ref(5);
const storage = ref<RuntimeStorageTarget | null>(null);
const remoteSetupOpen = ref(false);
const storageLoading = ref(false);
const storageSaving = ref(false);
const storageError = ref('');
const storageMessage = ref('');
const fingerprint = ref('');
const fingerprintConfirmed = ref(false);
const sshConfigMessage = ref('');
const privateKey = ref('');
const privateKeyPassphrase = ref('');
const credentialState = ref<{ state: string; provider: string; saved: boolean; loaded: boolean; message: string | null }>({
  state: 'checking', provider: 'unknown', saved: false, loaded: false, message: null,
});
const credentialBusy = ref(false);
const sudoPassword = ref('');
const sudoHelp = ref('');
const sudoRequired = ref(false);
const preflightMessage = ref('');
const preflightOk = ref(false);
const hostKeyBusy = ref(false);
const accessBusy = ref(false);
const installBusy = ref(false);
const remoteForm = ref({ host: '', ssh_port: 22, ssh_user: '' });
const remoteFormDraftKey = 'devassistant.runtime.remote-ssh-form.v1';
let remoteFormInitialized = false;

const persistRemoteFormDraft = () => {
  try {
    window.localStorage.setItem(remoteFormDraftKey, JSON.stringify(remoteForm.value));
  } catch {
    // Keep the setup form usable when browser storage is unavailable.
  }
};

const restoreRemoteFormDraft = () => {
  try {
    const raw = window.localStorage.getItem(remoteFormDraftKey);
    if (!raw) return;
    const draft = JSON.parse(raw);
    const port = Number(draft?.ssh_port);
    if (typeof draft?.host !== 'string' || typeof draft?.ssh_user !== 'string'
      || !Number.isInteger(port) || port < 1 || port > 65535) return;
    remoteFormInitialized = true;
    remoteForm.value = { host: draft.host, ssh_port: port, ssh_user: draft.ssh_user };
  } catch {
    // Ignore malformed or unavailable browser storage and use the saved target.
  }
};

watch(remoteForm, () => {
  remoteFormInitialized = true;
  persistRemoteFormDraft();
}, { deep: true });

watch(() => [remoteForm.value.host, remoteForm.value.ssh_port, remoteForm.value.ssh_user,
  fingerprint.value, fingerprintConfirmed.value, credentialState.value.state], () => {
  preflightOk.value = false;
  preflightMessage.value = '';
});

const fetchStorage = async () => {
  storageLoading.value = true;
  storageError.value = '';
  try {
    const response = await makeRequest('/api/runtime/settings/storage');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Runtime storage settings are unavailable.');
    storage.value = data as RuntimeStorageTarget;
    if (data.mode === 'remote') remoteSetupOpen.value = true;
    else if (data.mode === 'local') remoteSetupOpen.value = false;
    if (!remoteFormInitialized && data.remote_configured && data.host && data.ssh_user && data.ssh_port) {
      remoteFormInitialized = true;
      remoteForm.value = { host: data.host, ssh_port: data.ssh_port, ssh_user: data.ssh_user };
      persistRemoteFormDraft();
    }
  } catch (cause: any) {
    storageError.value = cause?.message || 'Runtime storage settings are unavailable.';
  } finally {
    storageLoading.value = false;
  }
};

const refresh = async () => {
  loading.value = true;
  error.value = '';
  try {
    const response = await makeRequest('/api/runtime/settings');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Runtime telemetry settings are unavailable.');
    settings.value = data as RuntimeTelemetrySettings;
    p95WindowHours.value = settings.value.p95_window_hours;
    refreshMinutes.value = settings.value.refresh_minutes;
  } catch (cause: any) {
    error.value = cause?.message || 'Runtime telemetry settings are unavailable.';
  } finally {
    loading.value = false;
  }
  await fetchStorage();
};

const remotePayload = () => ({
  ...remoteForm.value,
  host_fingerprint: fingerprint.value,
  sudo_password: sudoPassword.value,
});

const callCredentialHelper = async (method: 'GET' | 'POST' | 'DELETE', body?: Record<string, string>) => {
  const authResponse = await makeRequest('/api/runtime/setup-authorization', { method: 'POST' });
  const auth = await authResponse.json();
  if (!authResponse.ok || !auth.token) throw new Error(auth.detail || 'Runtime credential authorization is unavailable.');
  let response: Response;
  try {
    response = await fetch('http://127.0.0.1:47831/runtime-ssh-credentials', {
      method,
      headers: { Authorization: `Bearer ${auth.token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
  } catch {
    throw new Error('The desktop Runtime helper is not reachable. Start or restart Developer Assistant with `just up-dev`, then refresh credential status.');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'The desktop SSH credential helper is unavailable.');
  return data;
};

const resolveSshConfig = async () => {
  const authResponse = await makeRequest('/api/runtime/setup-authorization', { method: 'POST' });
  const auth = await authResponse.json();
  if (!authResponse.ok || !auth.token) throw new Error(auth.detail || 'Runtime SSH authorization is unavailable.');
  let response: Response;
  try {
    response = await fetch('http://127.0.0.1:47831/runtime-ssh-config', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(remoteForm.value),
    });
  } catch {
    throw new Error('The desktop Runtime helper is not reachable. Start or restart Developer Assistant with `just up-dev`, then retry.');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'SSH host configuration could not be resolved.');
  remoteForm.value = { host: data.host, ssh_port: data.ssh_port, ssh_user: data.ssh_user };
  sshConfigMessage.value = data.alias === data.host
    ? `Using SSH ${data.ssh_user}@${data.host}:${data.ssh_port}.`
    : `Resolved SSH alias “${data.alias}” to ${data.ssh_user}@${data.host}:${data.ssh_port}.`;
};

const refreshCredentialStatus = async () => {
  try {
    const current = await callCredentialHelper('GET');
    if (current.state === 'locked' || current.state === 'disconnected') {
      try {
        credentialState.value = await callCredentialHelper('POST', { action: 'reload' });
        return;
      } catch (cause: any) {
        credentialState.value = { ...current, message: cause?.message || current.message };
        return;
      }
    }
    credentialState.value = current;
  } catch (cause: any) {
    credentialState.value = { state: 'unavailable', provider: 'unknown', saved: false, loaded: false,
      message: cause?.message || 'Start the Developer Assistant desktop helper and retry.' };
  }
};

const saveCredential = async () => {
  credentialBusy.value = true;
  storageError.value = '';
  try {
    await callCredentialHelper('POST', { private_key: privateKey.value, passphrase: privateKeyPassphrase.value });
    privateKey.value = '';
    privateKeyPassphrase.value = '';
    await refreshCredentialStatus();
    storageMessage.value = 'SSH key saved in the desktop OS credential store and loaded for the Runtime tunnel.';
  } catch (cause: any) {
    storageError.value = cause?.message || 'SSH credential could not be saved.';
  } finally {
    credentialBusy.value = false;
  }
};

const removeCredential = async () => {
  if (!window.confirm('Remove the saved Runtime SSH key from the OS credential store?')) return;
  credentialBusy.value = true;
  storageError.value = '';
  try {
    await callCredentialHelper('DELETE');
    await refreshCredentialStatus();
    storageMessage.value = 'Saved Runtime SSH credential removed.';
  } catch (cause: any) {
    storageError.value = cause?.message || 'SSH credential could not be removed.';
  } finally {
    credentialBusy.value = false;
  }
};

const scanFingerprint = async () => {
  hostKeyBusy.value = true;
  storageError.value = '';
  storageMessage.value = '';
  fingerprintConfirmed.value = false;
  try {
    await resolveSshConfig();
    const response = await makeRequest('/api/runtime/settings/storage/host-key', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host: remoteForm.value.host, ssh_port: remoteForm.value.ssh_port }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'SSH host fingerprint could not be retrieved.');
    fingerprint.value = data.fingerprint;
    storageMessage.value = 'Fingerprint retrieved. Verify it with the remote host administrator before confirming.';
  } catch (cause: any) {
    storageError.value = cause?.message || 'SSH host fingerprint could not be retrieved.';
  } finally {
    hostKeyBusy.value = false;
  }
};

const checkRemoteAccess = async () => {
  accessBusy.value = true;
  storageError.value = '';
  storageMessage.value = '';
  sudoHelp.value = '';
  preflightMessage.value = '';
  try {
    const response = await makeRequest('/api/runtime/settings/storage/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(remotePayload()),
    });
    const data = await response.json();
    preflightOk.value = Boolean(response.ok && data.ok);
    sudoRequired.value = Boolean(data.password_required);
    preflightMessage.value = data.message || '';
    sudoHelp.value = data.help || data.storage_help || '';
    if (data.storage_ready === false) preflightOk.value = false;
    if (!response.ok && !data.message) throw new Error(data.detail || 'SSH and sudo checks failed.');
    if (preflightOk.value) storageMessage.value = data.message;
  } catch (cause: any) {
    preflightOk.value = false;
    storageError.value = cause?.message || 'SSH and sudo checks failed.';
  } finally {
    sudoPassword.value = '';
    accessBusy.value = false;
  }
};

const installRemote = async () => {
  installBusy.value = true;
  storageError.value = '';
  storageMessage.value = '';
  sudoHelp.value = '';
  try {
    const response = await makeRequest('/api/runtime/settings/storage/install', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(remotePayload()),
    });
    const data = await response.json();
    if (!response.ok) {
      sudoHelp.value = data.help || '';
      throw new Error(data.detail || 'Remote ClickHouse setup failed.');
    }
    storage.value = data.target as RuntimeStorageTarget;
    storageMessage.value = 'Remote ClickHouse is verified and selected. New Runtime writes use this target; the Collector stays running.';
    sudoPassword.value = '';
    preflightOk.value = false;
    await refresh();
  } catch (cause: any) {
    storageError.value = cause?.message || 'Remote ClickHouse setup failed.';
  } finally {
    sudoPassword.value = '';
    installBusy.value = false;
  }
};

const setStorageMode = async (mode: RuntimeStorageTarget['mode']) => {
  storageSaving.value = true;
  storageError.value = '';
  storageMessage.value = '';
  try {
    const response = await makeRequest('/api/runtime/settings/storage/mode', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Runtime storage mode could not be applied.');
    storage.value = data.target as RuntimeStorageTarget;
    if (mode === 'local' || mode === 'disabled') remoteSetupOpen.value = false;
    if (mode === 'remote') remoteSetupOpen.value = true;
    storageMessage.value = mode === 'disabled'
      ? 'Runtime telemetry storage is disabled. Existing data is preserved.'
      : `Runtime storage changed to ${mode}. New telemetry uses this target without restarting the Collector.`;
    await refresh();
  } catch (cause: any) {
    storageError.value = cause?.message || 'Runtime storage mode could not be applied.';
  } finally {
    storageSaving.value = false;
  }
};

const toggleLocalStorage = (enabled: boolean) => {
  void setStorageMode(enabled ? 'local' : 'disabled');
};

const toggleRemoteStorage = (enabled: boolean) => {
  if (!enabled && storage.value?.mode === 'remote') {
    void setStorageMode('disabled');
    return;
  }
  remoteSetupOpen.value = enabled;
  if (enabled && storage.value?.remote_configured) void setStorageMode('remote');
};

const savePolicy = async () => {
  saving.value = true;
  error.value = '';
  savedMessage.value = '';
  try {
    const response = await makeRequest('/api/runtime/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ p95_window_hours: p95WindowHours.value, refresh_minutes: refreshMinutes.value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Runtime sampling settings could not be saved.');
    settings.value = data as RuntimeTelemetrySettings;
    p95WindowHours.value = settings.value.p95_window_hours;
    refreshMinutes.value = settings.value.refresh_minutes;
    savedMessage.value = data.policy_refresh?.pending ? 'Saved; the Collector policy will refresh on its next cycle.' : 'Saved and applied to the Collector.';
  } catch (cause: any) {
    error.value = cause?.message || 'Runtime sampling settings could not be saved.';
  } finally {
    saving.value = false;
  }
};

onMounted(async () => {
  restoreRemoteFormDraft();
  await Promise.all([refresh(), refreshCredentialStatus()]);
});
</script>

<template>
  <section class="rounded-2xl border border-slate-800 bg-slate-900/30 p-5 space-y-4">
    <div class="flex items-start justify-between gap-3">
      <div>
        <h3 class="flex items-center gap-2 text-sm font-semibold text-white"><Activity class="h-4 w-4 text-cyan-400" /> Runtime telemetry</h3>
        <p class="mt-1 text-[11px] text-slate-500">Choose where Runtime telemetry is stored and tune how slow traces are retained.</p>
      </div>
      <button type="button" @click="refresh" :disabled="loading" class="rounded-lg border border-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-800"><RefreshCw class="mr-1 inline h-3 w-3" :class="loading ? 'animate-spin' : ''" />Refresh</button>
    </div>
    <div v-if="error" class="rounded-xl border border-rose-500/20 bg-rose-500/5 p-3 text-xs text-rose-300">{{ error }}</div>
    <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-4 space-y-4">
      <div class="flex items-center gap-2"><Server class="h-4 w-4 text-cyan-300" /><h4 class="text-xs font-semibold text-white">ClickHouse storage</h4></div>
      <div class="grid gap-2 sm:grid-cols-3" role="group" aria-label="Runtime ClickHouse storage mode">
        <button type="button" :aria-pressed="storage?.mode === 'disabled'" :disabled="storageLoading || storageSaving"
          @click="setStorageMode('disabled')"
          class="rounded-lg border px-3 py-2 text-xs capitalize transition"
          :class="storage?.mode === 'disabled' ? 'border-cyan-400/60 bg-cyan-500/10 text-cyan-100' : 'border-slate-700 text-slate-300 hover:bg-slate-800 disabled:opacity-50'">Disabled</button>
        <ToggleCard :model-value="storage?.mode === 'local'" label="Local" description="Store telemetry on this machine" :disabled="storageLoading || storageSaving" @update:model-value="toggleLocalStorage" />
        <ToggleCard :model-value="remoteSetupOpen || storage?.mode === 'remote'" label="Remote" description="Store telemetry on an SSH host" :disabled="storageLoading || storageSaving || storage?.mode === 'local'" @update:model-value="toggleRemoteStorage" />
      </div>
      <div v-if="storage" class="text-[11px] text-slate-400">
        Active: <span class="capitalize text-slate-200">{{ storage.mode }}</span>
        <span v-if="storage.host"> · {{ storage.host }}:{{ storage.http_port }}</span>
        · Health: <span class="text-slate-200">{{ storage.health }}</span>
        <span v-if="storage.verified_at"> · verified {{ new Date(storage.verified_at).toLocaleString() }}</span>
      </div>
      <div v-if="remoteSetupOpen && (storage?.mode !== 'remote' || storage?.health !== 'verified')" class="space-y-3 border-t border-slate-800 pt-4">
        <p class="text-[11px] text-slate-400">Remote setup uses SSH as the only network transport. ClickHouse listens on the remote loopback interface; the desktop helper stores the SSH key in the OS credential store and maintains a private tunnel. Existing local telemetry remains in the local database.</p>
        <div class="rounded-lg border border-slate-700 bg-slate-900/60 p-3 text-[11px]">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <span class="text-slate-200">Desktop SSH credential: <span class="capitalize">{{ credentialState.state }}</span><span v-if="credentialState.provider !== 'unknown'"> · {{ credentialState.provider }}</span></span>
            <button v-if="credentialState.saved" type="button" @click="removeCredential" :disabled="credentialBusy" class="rounded-md border border-rose-500/30 px-2 py-1 text-rose-200 disabled:opacity-50">Remove saved key</button>
          </div>
          <p v-if="credentialState.message" class="mt-1 text-amber-200">{{ credentialState.message }}</p>
          <button type="button" @click="refreshCredentialStatus" :disabled="credentialBusy" class="mt-2 rounded-md border border-slate-700 px-2 py-1 text-slate-300">Refresh credential status</button>
        </div>
        <div class="grid gap-3 sm:grid-cols-2">
          <label class="grid gap-1 text-[10px] text-slate-400">Remote host
            <input v-model="remoteForm.host" autocomplete="off" placeholder="clickhouse.example.com" class="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-xs text-slate-100" />
          </label>
          <label class="grid gap-1 text-[10px] text-slate-400">SSH port
            <input v-model.number="remoteForm.ssh_port" type="number" min="1" max="65535" class="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-xs text-slate-100" />
          </label>
          <label class="grid gap-1 text-[10px] text-slate-400">SSH user
            <input v-model="remoteForm.ssh_user" autocomplete="username" class="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-xs text-slate-100" />
          </label>
          <label class="grid gap-1 text-[10px] text-slate-400 sm:col-span-2">SSH private key
            <textarea v-model="privateKey" rows="4" autocomplete="off" spellcheck="false" class="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 font-mono text-[10px] text-slate-100" />
          </label>
        </div>
        <button type="button" @click="scanFingerprint" :disabled="hostKeyBusy || !remoteForm.host" class="rounded-lg border border-slate-700 px-3 py-2 text-[11px] text-slate-200 disabled:opacity-50">{{ hostKeyBusy ? 'Checking host…' : 'Retrieve SSH fingerprint' }}</button>
        <p v-if="sshConfigMessage" class="text-[10px] text-slate-400">{{ sshConfigMessage }}</p>
        <div v-if="fingerprint" class="rounded-lg border border-amber-500/20 bg-amber-500/5 p-3 text-[11px] text-amber-100">
          <p>SSH fingerprint: <code class="break-all">{{ fingerprint }}</code></p>
          <label class="mt-2 flex items-start gap-2"><input v-model="fingerprintConfirmed" type="checkbox" class="mt-0.5" /> I verified this fingerprint with the remote host administrator.</label>
        </div>
        <label v-if="privateKey" class="grid max-w-2xl gap-1 text-[10px] text-slate-400">SSH key passphrase (optional)
          <input v-model="privateKeyPassphrase" type="password" autocomplete="new-password" class="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-xs text-slate-100" />
        </label>
        <button v-if="privateKey" type="button" @click="saveCredential" :disabled="credentialBusy" class="rounded-lg border border-cyan-500/30 px-3 py-2 text-[11px] text-cyan-100 disabled:opacity-50">{{ credentialBusy ? 'Saving to OS credential store…' : 'Save SSH key securely' }}</button>
        <label v-if="fingerprintConfirmed" class="grid max-w-sm gap-1 text-[10px] text-slate-400">{{ sudoRequired ? 'Enter sudo password again for installation' : 'Sudo password (only if prompted by sudo)' }}
          <input v-model="sudoPassword" type="password" autocomplete="new-password" class="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-xs text-slate-100" />
        </label>
        <p v-if="fingerprintConfirmed && credentialState.state !== 'ready'" id="ssh-test-prerequisite" role="status" aria-live="polite" class="text-[10px] text-amber-200">
          {{ credentialState.state === 'missing' ? 'Save a valid SSH private key above before testing access.' : credentialState.state === 'locked' ? 'Unlock the desktop keyring, then refresh credential status before testing.' : credentialState.state === 'checking' ? 'Checking the desktop SSH credential…' : 'The desktop SSH credential is not ready. Refresh its status or save a valid key before testing.' }}
        </p>
        <div class="flex flex-wrap gap-2">
          <button type="button" @click="checkRemoteAccess" :disabled="accessBusy || credentialBusy || !fingerprintConfirmed || credentialState.state !== 'ready'" :aria-describedby="credentialState.state !== 'ready' && fingerprintConfirmed ? 'ssh-test-prerequisite' : undefined" class="rounded-lg border border-cyan-500/30 px-3 py-2 text-[11px] text-cyan-100 disabled:opacity-50">{{ accessBusy ? 'Checking SSH and sudo…' : 'Test SSH and sudo access' }}</button>
          <button type="button" @click="installRemote" :disabled="installBusy || !preflightOk || !fingerprintConfirmed || credentialState.state !== 'ready' || (sudoRequired && !sudoPassword)" class="rounded-lg bg-cyan-500/15 px-3 py-2 text-[11px] text-cyan-100 disabled:opacity-50">{{ installBusy ? 'Installing ClickHouse…' : 'Install & connect' }}</button>
        </div>
        <div v-if="preflightMessage" class="flex items-start gap-2 text-[11px]" :class="preflightOk ? 'text-emerald-300' : 'text-amber-200'"><ShieldCheck class="mt-0.5 h-3.5 w-3.5 shrink-0" />{{ preflightMessage }}</div>
        <div v-if="sudoHelp" class="rounded-lg border border-amber-500/20 bg-amber-500/5 p-3 text-[11px] text-amber-100">{{ sudoHelp }}</div>
      </div>
      <p v-if="remoteSetupOpen" class="text-[10px] text-slate-500">Allow inbound SSH only. ClickHouse HTTP (8123) and native (9000) listeners remain bound to remote loopback and are reached through the SSH tunnel. Switching storage never migrates or deletes data.</p>
      <details v-if="remoteSetupOpen" class="rounded-lg border border-slate-800 px-3 py-2 text-[10px] text-slate-400">
        <summary class="cursor-pointer text-slate-300">Remote setup recovery guide</summary>
        <ol class="mt-2 list-inside list-decimal space-y-1">
          <li>Confirm the SSH fingerprint with the remote administrator before testing access.</li>
          <li>If Keychain or Secret Service is locked, unlock it on the desktop and refresh credential status. Re-enter the key if it is missing.</li>
          <li>Allow inbound SSH on the configured port. Do not open ClickHouse ports 8123 or 9000 to the network.</li>
          <li>Provisioning supports Ubuntu and Debian. The SSH account needs sudo access; test SSH and sudo before installation.</li>
        </ol>
      </details>
      <div v-if="storageError" class="rounded-lg border border-rose-500/20 bg-rose-500/5 p-3 text-[11px] text-rose-300">{{ storageError }}</div>
      <div v-if="storageMessage" class="flex items-start gap-2 text-[11px] text-emerald-300"><CheckCircle2 class="mt-0.5 h-3.5 w-3.5 shrink-0" />{{ storageMessage }}</div>
    </div>
    <div v-if="settings" class="grid gap-3 sm:grid-cols-2">
      <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
        <p class="text-[10px] uppercase tracking-wide text-slate-500">Span store</p>
        <p class="mt-1 text-xs text-slate-200">{{ settings.store }} · {{ settings.enabled ? 'configured' : 'not configured' }}</p>
        <p class="mt-1 break-all font-mono text-[10px] text-slate-500">{{ settings.connection || 'No ClickHouse endpoint' }}</p>
      </div>
      <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
        <p class="text-[10px] uppercase tracking-wide text-slate-500">Retention</p>
        <p class="mt-1 text-xs text-slate-200">Sampled traces: {{ settings.retention_days }} days</p>
        <p class="mt-1 text-[10px] text-slate-500">Pre-tail aggregate input: {{ settings.aggregation_retention_days }} days.</p>
      </div>
      <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
        <p class="text-[10px] uppercase tracking-wide text-slate-500">Configured recording ratio</p>
        <p class="mt-1 text-xs text-slate-200">{{ settings.collector_baseline_percent }}%</p>
        <p class="mt-1 text-[10px] text-slate-500">Baseline sampling after spans reach the Collector. PHP SDK sampling happens first and may reduce this input.</p>
      </div>
      <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
        <p class="text-[10px] uppercase tracking-wide text-slate-500">OTLP receiver</p>
        <p class="mt-1 break-all font-mono text-[10px] text-slate-200">{{ settings.otlp_endpoint || 'Not configured' }}</p>
        <p class="mt-1 text-[10px] text-slate-500">Identity scope: {{ settings.scope }}</p>
      </div>
      <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3 sm:col-span-2">
        <p class="text-[10px] uppercase tracking-wide text-slate-500">Slow trace policy</p>
        <p class="mt-1 text-[10px] text-slate-400">Among received spans, errors and requests slower than the rolling p95 are retained, along with the baseline sample. Upstream SDK sampling may omit spans before they reach this policy.</p>
        <div class="mt-3 flex flex-wrap items-end gap-3">
          <label class="grid gap-1 text-[10px] text-slate-400">P95 window (hours)
            <input v-model.number="p95WindowHours" type="number" min="1" :max="settings.aggregation_retention_hours" class="w-28 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1.5 text-xs text-slate-100" />
          </label>
          <label class="grid gap-1 text-[10px] text-slate-400">Policy refresh (minutes)
            <input v-model.number="refreshMinutes" type="number" min="1" max="60" class="w-28 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1.5 text-xs text-slate-100" />
          </label>
          <button type="button" @click="savePolicy" :disabled="saving || loading" class="rounded-lg border border-cyan-500/30 px-3 py-1.5 text-[10px] text-cyan-200 hover:bg-cyan-500/10 disabled:opacity-50"><Save class="mr-1 inline h-3 w-3" />{{ saving ? 'Saving…' : 'Save policy' }}</button>
          <span v-if="savedMessage" class="text-[10px] text-emerald-300">{{ savedMessage }}</span>
        </div>
        <p class="mt-2 text-[10px] text-slate-500">Window: 1 to {{ settings.aggregation_retention_hours }} hours. Refresh: 1 to 60 minutes.</p>
      </div>
    </div>
  </section>
</template>
