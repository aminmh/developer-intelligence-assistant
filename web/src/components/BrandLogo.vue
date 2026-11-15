<script setup lang="ts">
import { useId } from 'vue';

// "Code spark": code brackets with an AI spark between them.
// The gradient follows the theme through --shell-grad-* when rendered inside .app-shell.
const gradientId = `brand-spark-${useId()}`;
</script>

<template>
  <svg viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <defs>
      <linearGradient :id="gradientId" x1="6" y1="6" x2="42" y2="42" gradientUnits="userSpaceOnUse">
        <stop style="stop-color: rgb(var(--shell-grad-a-rgb, 34 211 238))" />
        <stop offset="1" style="stop-color: var(--accent-primary, #a78bfa)" />
      </linearGradient>
    </defs>
    <rect x="1.5" y="1.5" width="45" height="45" rx="13" fill="#0d1226" :stroke="`url(#${gradientId})`" stroke-opacity=".55" />
    <path
      d="M17 14 8.5 24 17 34M31 14 39.5 24 31 34"
      :stroke="`url(#${gradientId})`"
      stroke-width="3.4"
      stroke-linecap="round"
      stroke-linejoin="round"
    />
    <path
      class="brand-logo-spark"
      d="M24 15.5c.9 4.6 2.9 6.6 7.5 7.5-4.6.9-6.6 2.9-7.5 7.5-.9-4.6-2.9-6.6-7.5-7.5 4.6-.9 6.6-2.9 7.5-7.5Z"
      :fill="`url(#${gradientId})`"
    />
  </svg>
</template>
