<script setup lang="ts">
/**
 * Personal IDE tokens for the Developer Assistant VS Code extension.
 * Spec: .archcore/frontend-assist/contract-change-notifications.spec.md
 */
import { onMounted, ref } from 'vue';
import { Copy, KeyRound, Plus, Trash2 } from 'lucide-vue-next';
import { toastMessage } from '../../composables/useToasts';

type TokenRow = { token_id: string; name: string; created_at: string; expires_at: string; last_used_at: string | null };

const tokens = ref<TokenRow[]>([]);
const name = ref('VS Code');
const created = ref<string | null>(null);
const busy = ref(false);
const error = toastMessage('error');
const message = toastMessage('success');

const date = (value: string | null) => (value ? new Date(value).toLocaleString() : 'never');

async function load() {
  const response = await fetch('/api/ide-tokens');
  const data = await response.json().catch(() => ({}));
  if (!response.ok) { error.value = data.detail || 'IDE tokens could not be loaded.'; return; }
  tokens.value = data.tokens || [];
}

async function create() {
  busy.value = true;
  try {
    const response = await fetch('/api/ide-tokens', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.value }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { error.value = data.detail || 'The token could not be created.'; return; }
    created.value = data.token;
    await load();
  } finally { busy.value = false; }
}

async function revoke(row: TokenRow) {
  const response = await fetch(`/api/ide-tokens/${row.token_id}`, { method: 'DELETE' });
  if (!response.ok) { error.value = 'The token could not be revoked.'; return; }
  message.value = `Revoked “${row.name}”. Extensions using it stop at their next request.`;
  await load();
}

async function copy() {
  if (!created.value) return;
  await navigator.clipboard.writeText(created.value);
  message.value = 'Token copied. Paste it into “Developer Assistant: Sign in” in VS Code.';
}

onMounted(load);
</script>

<template>
  <section class="rounded-2xl border border-slate-800 bg-slate-900/40 p-5 space-y-4">
    <div>
      <h3 class="text-lg font-bold text-white flex items-center gap-2"><KeyRound class="w-5 h-5 text-cyan-400" /> IDE tokens</h3>
      <p class="text-xs text-slate-400 mt-1">
        The VS Code extension uses a personal token to warn you, inside the editor, when a back-end change to an API
        contract affects your front-end project. A token reads contract changes for the projects you can access, and
        nothing else. It expires after 90 days.
      </p>
    </div>

    <form class="flex items-end gap-3" @submit.prevent="create">
      <label class="text-xs text-slate-400 flex-1">Token name
        <input v-model="name" maxlength="80" class="field" placeholder="VS Code on my laptop" />
      </label>
      <button type="submit" :disabled="busy" class="px-3 py-2 rounded-lg bg-indigo-600 text-white text-xs font-semibold disabled:opacity-50">
        <Plus class="w-3.5 h-3.5 inline mr-1" />Create token
      </button>
    </form>

    <div v-if="created" class="rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 space-y-2" role="status">
      <p class="text-xs text-amber-200 font-semibold">Copy this token now. It is not shown again.</p>
      <div class="flex items-center gap-2">
        <code class="flex-1 truncate rounded-lg bg-slate-950/70 px-3 py-2 text-xs text-slate-100 font-mono">{{ created }}</code>
        <button type="button" class="px-3 py-2 rounded-lg border border-slate-700 text-xs text-slate-200" @click="copy">
          <Copy class="w-3.5 h-3.5 inline mr-1" />Copy
        </button>
      </div>
    </div>

    <table v-if="tokens.length" class="w-full text-xs text-slate-300">
      <thead class="text-left text-slate-500">
        <tr><th class="py-1.5 font-medium">Name</th><th class="font-medium">Created</th><th class="font-medium">Last used</th><th class="font-medium">Expires</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="row in tokens" :key="row.token_id" class="border-t border-slate-800">
          <td class="py-2">{{ row.name }}</td>
          <td>{{ date(row.created_at) }}</td>
          <td>{{ date(row.last_used_at) }}</td>
          <td>{{ date(row.expires_at) }}</td>
          <td class="text-right">
            <button type="button" class="text-rose-300 hover:text-rose-200" :aria-label="`Revoke ${row.name}`" @click="revoke(row)">
              <Trash2 class="w-3.5 h-3.5 inline" /> Revoke
            </button>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="text-xs text-slate-500">You have no IDE tokens.</p>
  </section>
</template>

<style scoped>.field{display:block;width:100%;margin-top:.35rem;border-radius:.6rem;border:1px solid rgb(51 65 85);background:#0f172a;color:#e2e8f0;padding:.55rem .7rem;font-size:.75rem}.field:focus{outline:2px solid rgb(99 102 241 / .45)}</style>
