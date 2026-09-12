import { computed, onMounted, reactive, ref, watch } from 'vue';
import type {
  AppearanceAccent,
  AppearanceMode,
  AppearancePalette,
  AppearancePreferences,
  NeonBorderTheme,
} from '../types';

const STORAGE_KEY = 'dev-assistant-appearance';

const DEFAULT_PREFERENCES: AppearancePreferences = {
  mode: 'dark',
  palette: 'default',
  accent: 'indigo',
  neonBorder: 'cyan',
  showSystemMonitor: true,
  showWelcomeBanner: true,
};

const isMode = (value: unknown): value is AppearanceMode =>
  value === 'dark' || value === 'light' || value === 'system';

const isAccent = (value: unknown): value is AppearanceAccent =>
  value === 'indigo' || value === 'cyan' || value === 'emerald' || value === 'orange' || value === 'rose';

const isPalette = (value: unknown): value is AppearancePalette =>
  value === 'default' ||
  value === 'forest-teal' ||
  value === 'ember-violet' ||
  value === 'graphite-sage' ||
  value === 'royal-lilac' ||
  value === 'ember-copper' ||
  value === 'black-emerald' ||
  value === 'midnight-cobalt' ||
  value === 'deep-teal-plum' ||
  value === 'monochrome' ||
  value === 'obsidian-plum' ||
  value === 'black-coffee';

const isNeonBorder = (value: unknown): value is NeonBorderTheme =>
  value === 'cyan' || value === 'violet' || value === 'amber' || value === 'rose';

export const useAppearancePreferences = () => {
  const preferences = reactive<AppearancePreferences>({ ...DEFAULT_PREFERENCES });
  const hydrated = ref(false);
  const systemPrefersLight = ref(false);

  const resolvedTheme = computed<'dark' | 'light'>(() => {
    if (preferences.mode === 'system') {
      return systemPrefersLight.value ? 'light' : 'dark';
    }
    return preferences.mode;
  });

  const updatePreference = <K extends keyof AppearancePreferences>(
    key: K,
    value: AppearancePreferences[K],
  ) => {
    preferences[key] = value;
  };

  const reset = () => {
    Object.assign(preferences, DEFAULT_PREFERENCES);
  };

  onMounted(() => {
    const mediaQuery = window.matchMedia('(prefers-color-scheme: light)');
    systemPrefersLight.value = mediaQuery.matches;

    const handleSystemThemeChange = (event: MediaQueryListEvent) => {
      systemPrefersLight.value = event.matches;
    };
    mediaQuery.addEventListener('change', handleSystemThemeChange);

    try {
      const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null') as Partial<AppearancePreferences> | null;
      if (stored && typeof stored === 'object') {
        if (isMode(stored.mode)) preferences.mode = stored.mode;
        if (isPalette(stored.palette)) preferences.palette = stored.palette;
        if (isAccent(stored.accent)) preferences.accent = stored.accent;
        if (isNeonBorder(stored.neonBorder)) preferences.neonBorder = stored.neonBorder;
        if (typeof stored.showSystemMonitor === 'boolean') preferences.showSystemMonitor = stored.showSystemMonitor;
        if (typeof stored.showWelcomeBanner === 'boolean') preferences.showWelcomeBanner = stored.showWelcomeBanner;
      }
    } catch {
      // Invalid local preferences fall back to the defaults above.
    }

    hydrated.value = true;
  });

  watch(
    preferences,
    (value) => {
      if (hydrated.value) {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
      }
    },
    { deep: true },
  );

  return {
    preferences,
    resolvedTheme,
    updatePreference,
    reset,
  };
};
