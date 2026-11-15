import { onMounted, onUnmounted, ref, watch } from 'vue';

/** Keeps one view's selected tab in the shared query string and browser history. */
export function useUrlTab<T extends string>(options: { values: readonly T[]; fallback: T; queryKey?: string }) {
  const queryKey = options.queryKey || 'panel';
  const read = (): T => {
    const value = new URLSearchParams(window.location.search).get(queryKey);
    return options.values.includes(value as T) ? value as T : options.fallback;
  };
  const active = ref<T>(read());
  let syncingFromHistory = false;

  watch(active, (value) => {
    if (syncingFromHistory) return;
    const url = new URL(window.location.href);
    if (value === options.fallback) url.searchParams.delete(queryKey);
    else url.searchParams.set(queryKey, value);
    window.history.pushState(window.history.state, '', url);
  }, { flush: 'sync' });

  const syncFromHistory = () => {
    syncingFromHistory = true;
    active.value = read();
    syncingFromHistory = false;
  };
  onMounted(() => window.addEventListener('popstate', syncFromHistory));
  onUnmounted(() => window.removeEventListener('popstate', syncFromHistory));
  return active;
}
