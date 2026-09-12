<script setup lang="ts">
import { Activity, Monitor, Moon, Palette, Sparkles, Sun } from 'lucide-vue-next';
import type {
  AppearanceAccent,
  AppearanceMode,
  AppearancePalette,
  AppearancePreferences,
  NeonBorderTheme,
} from '../../types';

defineProps<{
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
  <section class="bg-slate-900/40 border border-slate-800 rounded-2xl p-6 space-y-5">
    <div>
      <h3 class="text-lg font-bold text-white flex items-center gap-2">
        <Palette class="w-5 h-5 text-cyan-400" />
        Appearance
      </h3>
      <p class="text-xs text-slate-400 mt-1">Customize the workspace look and choose which chat panels stay visible. These preferences are saved only in this browser.</p>
    </div>

    <div>
      <p class="text-[10px] uppercase tracking-wider text-slate-500 font-mono mb-2">Color palette</p>
      <div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-2">
        <button
          v-for="option in paletteOptions"
          :key="option.value"
          type="button"
          class="rounded-xl border p-2 text-left transition-all"
          :class="appearance.palette === option.value ? 'border-white/80 bg-white/10 shadow-lg' : 'border-slate-700 bg-slate-900/20 hover:border-slate-500'"
          :aria-pressed="appearance.palette === option.value"
          @click="selectPalette(option)"
        >
          <span class="block h-8 rounded-lg" :style="{ background: `linear-gradient(120deg, ${option.colors.join(', ')})` }"></span>
          <span class="block mt-2 text-[10px] font-semibold" :class="appearance.palette === option.value ? 'text-white' : 'text-slate-300'">{{ option.label }}</span>
          <span class="flex gap-1 mt-1.5">
            <span v-for="color in option.colors" :key="color" class="w-2 h-2 rounded-full" :style="{ backgroundColor: color }"></span>
          </span>
        </button>
      </div>
    </div>

    <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
      <div>
        <p class="text-[10px] uppercase tracking-wider text-slate-500 font-mono mb-2">Theme mode</p>
        <div class="grid grid-cols-3 gap-2">
          <button
            v-for="option in modeOptions"
            :key="option.value"
            type="button"
            class="flex flex-col items-center gap-1.5 rounded-xl border px-2 py-2 text-[10px] transition-colors"
            :class="appearance.mode === option.value ? 'border-cyan-400/70 bg-cyan-500/10 text-cyan-200' : 'border-slate-700 text-slate-400 hover:border-slate-500 hover:text-slate-200'"
            :aria-pressed="appearance.mode === option.value"
            @click="update('mode', option.value)"
          >
            <component :is="option.icon" class="w-4 h-4" />
            {{ option.label }}
          </button>
        </div>
      </div>

      <div>
        <p class="text-[10px] uppercase tracking-wider text-slate-500 font-mono mb-2">Accent color</p>
        <div class="flex flex-wrap gap-2">
          <button
            v-for="option in accentOptions"
            :key="option.value"
            type="button"
            class="inline-flex items-center gap-2 rounded-xl border px-2.5 py-2 text-[10px] transition-colors"
            :class="appearance.accent === option.value ? 'border-white/70 bg-white/10 text-white' : 'border-slate-700 text-slate-400 hover:border-slate-500 hover:text-slate-200'"
            :aria-pressed="appearance.accent === option.value"
            @click="update('accent', option.value)"
          >
            <span class="w-2.5 h-2.5 rounded-full" :style="{ backgroundColor: option.color, boxShadow: `0 0 8px ${option.color}` }"></span>
            {{ option.label }}
          </button>
        </div>
      </div>

      <div>
        <p class="text-[10px] uppercase tracking-wider text-slate-500 font-mono mb-2">Neon borders</p>
        <div class="flex flex-wrap gap-2">
          <button
            v-for="option in neonOptions"
            :key="option.value"
            type="button"
            class="inline-flex items-center gap-2 rounded-xl border px-2.5 py-2 text-[10px] transition-colors"
            :class="appearance.neonBorder === option.value ? 'border-white/70 bg-white/10 text-white' : 'border-slate-700 text-slate-400 hover:border-slate-500 hover:text-slate-200'"
            :aria-pressed="appearance.neonBorder === option.value"
            @click="update('neonBorder', option.value)"
          >
            <span class="w-2.5 h-2.5 rounded-full" :style="{ backgroundColor: option.color, boxShadow: `0 0 8px ${option.color}` }"></span>
            {{ option.label }}
          </button>
        </div>
      </div>
    </div>

    <div class="border-t border-slate-800 pt-4 grid grid-cols-1 sm:grid-cols-2 gap-3">
      <button
        type="button"
        class="flex items-center justify-between gap-3 rounded-xl border px-3 py-3 text-left transition-colors"
        :class="appearance.showWelcomeBanner ? 'border-emerald-400/40 bg-emerald-500/5' : 'border-slate-700 bg-slate-900/30'"
        :aria-pressed="appearance.showWelcomeBanner"
        @click="update('showWelcomeBanner', !appearance.showWelcomeBanner)"
      >
        <span class="flex items-center gap-2 min-w-0">
          <Sparkles class="w-4 h-4 text-cyan-300 shrink-0" />
          <span>
            <span class="block text-xs font-semibold text-slate-200">Welcome banner</span>
            <span class="block text-[10px] text-slate-500 mt-0.5">Animated terminal introduction</span>
          </span>
        </span>
        <span class="text-[10px] font-mono" :class="appearance.showWelcomeBanner ? 'text-emerald-300' : 'text-slate-500'">{{ appearance.showWelcomeBanner ? 'On' : 'Off' }}</span>
      </button>

      <button
        type="button"
        class="flex items-center justify-between gap-3 rounded-xl border px-3 py-3 text-left transition-colors"
        :class="appearance.showSystemMonitor ? 'border-emerald-400/40 bg-emerald-500/5' : 'border-slate-700 bg-slate-900/30'"
        :aria-pressed="appearance.showSystemMonitor"
        @click="update('showSystemMonitor', !appearance.showSystemMonitor)"
      >
        <span class="flex items-center gap-2 min-w-0">
          <Activity class="w-4 h-4 text-emerald-300 shrink-0" />
          <span>
            <span class="block text-xs font-semibold text-slate-200">System monitor</span>
            <span class="block text-[10px] text-slate-500 mt-0.5">CPU, RAM, temperature, and GPU</span>
          </span>
        </span>
        <span class="text-[10px] font-mono" :class="appearance.showSystemMonitor ? 'text-emerald-300' : 'text-slate-500'">{{ appearance.showSystemMonitor ? 'On' : 'Off' }}</span>
      </button>
    </div>
  </section>
</template>
