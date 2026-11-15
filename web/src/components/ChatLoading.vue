<script setup lang="ts">
import { Sparkles } from 'lucide-vue-next';

defineProps<{
  label?: string;
}>();
</script>

<template>
  <div class="chat-progress chat-loading" role="status" aria-live="polite">
    <Sparkles aria-hidden="true" />
    <span>{{ label || 'Working' }}</span>
    <span class="chat-loading-dots" aria-hidden="true"><i></i><i></i><i></i></span>
  </div>
</template>
