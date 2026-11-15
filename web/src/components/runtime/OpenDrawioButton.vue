<script setup lang="ts">
import { ref } from 'vue';
import { ExternalLink } from 'lucide-vue-next';
import { editorUrl } from '../../utils/drawioBridge';
defineProps<{ buttonClass?: string }>();
const error = ref('');
function open() {
  error.value = '';
  try {
    const url = editorUrl(import.meta.env.VITE_DRAWIO_ORIGIN || '', window.location.origin);
    // Standalone editing has no draft or application message bridge.
    const popup = window.open(url.origin, '_blank');
    if (popup) popup.opener = null;
    else error.value = 'The browser blocked Draw.io. Allow popups and try again.';
  } catch (cause: any) { error.value = cause.message || 'Configure the Draw.io service in application setup.'; }
}
</script>
<template>
  <div class="drawio-launch">
    <button type="button" :class="buttonClass || 'sc-btn'" @click="open"><ExternalLink aria-hidden="true" />Open Draw.io</button>
    <p v-if="error" role="alert">{{ error }} Set up the Draw.io service and reload the application.</p>
  </div>
</template>
<style scoped>
.drawio-launch { display: inline-flex; flex-wrap: wrap; align-items: center; gap: .5rem; }
.drawio-launch p { flex-basis: 100%; margin: 0; max-width: 34rem; font-size: .75rem; color: var(--color-amber-400, #fbbf24); }
</style>
