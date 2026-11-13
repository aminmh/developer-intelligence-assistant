<script setup lang="ts">
import { computed } from 'vue';
import { Activity, Check, Code, Gauge, Monitor, Moon, PanelsTopLeft, Sparkles, SwatchBook, Sun, Type } from 'lucide-vue-next';
import type {
  AppearanceAccent,
  AppearanceMode,
  AppearancePalette,
  AppearancePreferences,
  CodeFontFamily,
  NeonBorderTheme,
  UiFontFamily,
} from '../../types';
import ToggleCard from '../ToggleCard.vue';

const props = defineProps<{
  appearance: AppearancePreferences;
}>();

const emit = defineEmits<{
  (e: 'update', key: keyof AppearancePreferences, value: AppearancePreferences[keyof AppearancePreferences]): void;
}>();

const modeOptions: Array<{ value: AppearanceMode; label: string; icon: typeof Sun }> = [
  { value: 'dark', label: 'Dark', icon: Moon },
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'system', label: 'System', icon: Monitor },
];

const accentOptions: Array<{ value: AppearanceAccent; label: string; color: string }> = [
  { value: 'indigo', label: 'Indigo', color: '#818cf8' },
  { value: 'cyan', label: 'Cyan', color: '#22d3ee' },
  { value: 'emerald', label: 'Emerald', color: '#34d399' },
  { value: 'orange', label: 'Orange', color: '#f97316' },
  { value: 'rose', label: 'Rose', color: '#fb7185' },
];

const neonOptions: Array<{ value: NeonBorderTheme; label: string; color: string }> = [
  { value: 'cyan', label: 'Cyan', color: '#67e8f9' },
  { value: 'violet', label: 'Violet', color: '#c4b5fd' },
  { value: 'amber', label: 'Amber', color: '#fcd34d' },
  { value: 'emerald', label: 'Emerald', color: '#8bbb92' },
  { value: 'rose', label: 'Rose', color: '#fda4af' },
];

const uiFontOptions: UiFontFamily[] = ['Inter', 'IBM Plex Sans'];
const codeFontOptions: CodeFontFamily[] = ['Fira Code', 'JetBrains Mono'];

const paletteOptions: Array<{
  value: AppearancePalette;
  label: string;
  colors: string[];
  accent: AppearanceAccent;
  neon: NeonBorderTheme;
}> = [
  { value: 'default', label: 'Current Dark', colors: ['#080b16', '#0d1224', '#0b1825'], accent: 'indigo', neon: 'cyan' },
  { value: 'forest-teal', label: 'Forest Teal', colors: ['#092328', '#12544f', '#2a835f', '#8bbb92'], accent: 'emerald', neon: 'emerald' },
  { value: 'ember-violet', label: 'Ember Violet', colors: ['#211832', '#412b6b', '#5c3e94', '#f25912'], accent: 'orange', neon: 'violet' },
  { value: 'graphite-sage', label: 'Graphite Sage', colors: ['#1e201e', '#3c3d37', '#697565', '#ecdfcc'], accent: 'emerald', neon: 'amber' },
  { value: 'royal-lilac', label: 'Royal Lilac', colors: ['#17153b', '#2e236c', '#433d8b', '#c8acd6'], accent: 'indigo', neon: 'violet' },
  { value: 'ember-copper', label: 'Ember Copper', colors: ['#0c0c0c', '#481e14', '#9b3922', '#f2613f'], accent: 'orange', neon: 'rose' },
  { value: 'black-emerald', label: 'Black Emerald', colors: ['#0f0f0f', '#232d3f', '#005b41', '#008170'], accent: 'emerald', neon: 'emerald' },
  { value: 'midnight-cobalt', label: 'Midnight Cobalt', colors: ['#151e3f', '#19254d', '#1f2c5c', '#24336d', '#293a7a'], accent: 'indigo', neon: 'cyan' },
  { value: 'deep-teal-plum', label: 'Deep Teal Plum', colors: ['#006466', '#065a60', '#0b525b', '#144552', '#1b3a4b', '#212f45', '#272640', '#312244', '#3e1f47', '#4d194d'], accent: 'cyan', neon: 'violet' },
  { value: 'monochrome', label: 'Monochrome', colors: ['#6c6c6c', '#545454', '#373737', '#1e1e1e', '#000000'], accent: 'indigo', neon: 'cyan' },
  { value: 'obsidian-plum', label: 'Obsidian Plum', colors: ['#09050a', '#140e16', '#1f1520', '#291f2b', '#342936'], accent: 'rose', neon: 'violet' },
  { value: 'black-coffee', label: 'Black Coffee', colors: ['#090909', '#151412', '#1d1a17', '#100005', '#120604'], accent: 'orange', neon: 'rose' },
];

const glowLabel = computed(() => neonOptions.find((option) => option.value === props.appearance.neonBorder)?.label ?? '');
const accentLabel = computed(() => accentOptions.find((option) => option.value === props.appearance.accent)?.label ?? '');

const update = <K extends keyof AppearancePreferences>(key: K, value: AppearancePreferences[K]) => {
  emit('update', key, value);
};

const selectPalette = (option: (typeof paletteOptions)[number]) => {
  update('palette', option.value);
  update('accent', option.accent);
  update('neonBorder', option.neon);
};
</script>

<template>
  <div class="st-appearance">
    <section class="st-card">
      <h3><SwatchBook />Theme</h3>
      <p class="st-label">Mode</p>
      <div class="st-seg" role="group" aria-label="Theme mode">
        <button
          v-for="option in modeOptions"
          :key="option.value"
          type="button"
          :aria-pressed="appearance.mode === option.value"
          @click="update('mode', option.value)"
        >
          <component :is="option.icon" />
          {{ option.label }}
        </button>
      </div>

      <p class="st-label">Background palette</p>
      <p class="st-hint st-m0">Choosing a palette also sets its matching glow and accent colours.</p>
      <div class="st-palettes">
        <button
          v-for="option in paletteOptions"
          :key="option.value"
          type="button"
          class="st-palette"
          :aria-pressed="appearance.palette === option.value"
          @click="selectPalette(option)"
        >
          <span class="st-palette-swatch" :style="{ background: `linear-gradient(120deg, ${option.colors.join(', ')})` }"></span>
          <span class="st-palette-name">
            {{ option.label }}
            <Check v-if="appearance.palette === option.value" />
          </span>
        </button>
      </div>
    </section>

    <section class="st-card">
      <h3><Sparkles />Neon gradient</h3>
      <p class="st-hint">Your two colours make the gradient used for buttons, highlights and glows.</p>
      <div class="st-colors">
        <div>
          <p class="st-label">Glow · gradient start</p>
          <div class="st-dots" role="group" aria-label="Glow colour">
            <button
              v-for="option in neonOptions"
              :key="option.value"
              type="button"
              class="st-dot"
              :title="option.label"
              :aria-label="option.label"
              :aria-pressed="appearance.neonBorder === option.value"
              :style="{ '--st-dot': option.color }"
              @click="update('neonBorder', option.value)"
            ><i></i></button>
          </div>
        </div>
        <div>
          <p class="st-label">Accent · gradient end</p>
          <div class="st-dots" role="group" aria-label="Accent colour">
            <button
              v-for="option in accentOptions"
              :key="option.value"
              type="button"
              class="st-dot"
              :title="option.label"
              :aria-label="option.label"
              :aria-pressed="appearance.accent === option.value"
              :style="{ '--st-dot': option.color }"
              @click="update('accent', option.value)"
            ><i></i></button>
          </div>
        </div>
      </div>
      <div class="st-gradbar" aria-hidden="true">
        <span>{{ glowLabel }}</span>
        <i></i>
        <span>{{ accentLabel }}</span>
      </div>
    </section>

    <section class="st-card">
      <h3><Type />Fonts</h3>
      <div class="st-fonts">
        <label class="st-font">
          <span class="st-font-name"><Type />App UI</span>
          <select
            :value="appearance.uiFontFamily"
            class="st-input"
            @change="update('uiFontFamily', ($event.target as HTMLSelectElement).value as UiFontFamily)"
          >
            <option v-for="font in uiFontOptions" :key="font" :value="font">{{ font }}</option>
          </select>
          <span class="st-font-sample" :style="{ fontFamily: `'${appearance.uiFontFamily}', sans-serif` }">Panels, menus and general text.</span>
        </label>
        <label class="st-font">
          <span class="st-font-name"><Code />Code</span>
          <select
            :value="appearance.codeFontFamily"
            class="st-input"
            @change="update('codeFontFamily', ($event.target as HTMLSelectElement).value as CodeFontFamily)"
          >
            <option v-for="font in codeFontOptions" :key="font" :value="font">{{ font }}</option>
          </select>
          <span class="st-font-sample" :style="{ fontFamily: `'${appearance.codeFontFamily}', monospace` }">const total = items.sum('price');</span>
        </label>
      </div>
    </section>

    <section class="st-card">
      <h3><PanelsTopLeft />Panels and performance</h3>
      <div class="st-toggles">
        <ToggleCard :model-value="appearance.showWelcomeBanner" label="Welcome screen" description="Greeting and starter prompts in a new chat" @update:model-value="update('showWelcomeBanner', $event)">
          <template #icon><Sparkles class="st-toggle-icon" /></template>
        </ToggleCard>
        <ToggleCard :model-value="appearance.showSystemMonitor" label="System monitor" description="CPU, RAM, temperature and GPU above chat" @update:model-value="update('showSystemMonitor', $event)">
          <template #icon><Activity class="st-toggle-icon" /></template>
        </ToggleCard>
        <ToggleCard :model-value="appearance.lowPerformanceMode" label="Low-power mode" description="Stops looping animations and glass blur; refreshes metrics every 10 seconds" @update:model-value="update('lowPerformanceMode', $event)">
          <template #icon><Gauge class="st-toggle-icon" /></template>
        </ToggleCard>
      </div>
    </section>
  </div>
</template>
