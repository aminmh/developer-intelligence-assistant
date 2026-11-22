import { customRef, ref, type Ref } from 'vue';

export type ToastKind = 'success' | 'error' | 'info';

export interface Toast {
  id: number;
  kind: ToastKind;
  title: string;
  message: string;
  /** Times the same message was raised while this toast was visible. */
  count: number;
  /** Milliseconds left before it closes; frozen while the pointer or focus is on it. */
  remaining: number;
  paused: boolean;
}

/** How long a toast stays when nobody is hovering or focusing it. */
export const TOAST_DURATION_MS = 5000;
const MAX_TOASTS = 5;
const DEFAULT_TITLES: Record<ToastKind, string> = { success: 'Done', error: 'Something went wrong', info: 'Note' };

// One stack for the whole app: every page raises toasts here.
const toasts = ref<Toast[]>([]);
const timers = new Map<number, { handle: ReturnType<typeof setTimeout>; startedAt: number }>();
let nextId = 1;

const clearTimer = (id: number) => {
  const timer = timers.get(id);
  if (timer) clearTimeout(timer.handle);
  timers.delete(id);
};

const startTimer = (toast: Toast) => {
  clearTimer(toast.id);
  timers.set(toast.id, { handle: setTimeout(() => dismiss(toast.id), toast.remaining), startedAt: Date.now() });
};

function dismiss(id: number) {
  clearTimer(id);
  toasts.value = toasts.value.filter((toast) => toast.id !== id);
}

/** Pointer or keyboard focus entered the toast: keep it until it leaves. */
function pause(id: number) {
  const toast = toasts.value.find((item) => item.id === id);
  const timer = timers.get(id);
  if (!toast || toast.paused) return;
  if (timer) toast.remaining = Math.max(0, toast.remaining - (Date.now() - timer.startedAt));
  clearTimer(id);
  toast.paused = true;
}

/** Pointer or focus left: start a fresh 5 s so the reader isn't cut off mid-sentence. */
function resume(id: number) {
  const toast = toasts.value.find((item) => item.id === id);
  if (!toast || !toast.paused) return;
  toast.paused = false;
  toast.remaining = TOAST_DURATION_MS;
  startTimer(toast);
}

function show(kind: ToastKind, message: string, title?: string): number | null {
  const text = String(message ?? '').trim();
  if (!text) return null;
  const heading = title || DEFAULT_TITLES[kind];
  // The same message raised again (polling, retries) bumps a counter instead of stacking.
  const same = toasts.value.find((toast) => toast.kind === kind && toast.message === text && toast.title === heading);
  if (same) {
    same.count += 1;
    same.remaining = TOAST_DURATION_MS;
    if (!same.paused) startTimer(same);
    return same.id;
  }
  const toast: Toast = { id: nextId++, kind, title: heading, message: text, count: 1, remaining: TOAST_DURATION_MS, paused: false };
  toasts.value = [...toasts.value, toast];
  while (toasts.value.length > MAX_TOASTS) dismiss(toasts.value[0].id);
  startTimer(toasts.value.find((item) => item.id === toast.id)!);
  return toast.id;
}

export const toast = {
  success: (message: string, title?: string) => show('success', message, title),
  error: (message: string, title?: string) => show('error', message, title),
  info: (message: string, title?: string) => show('info', message, title),
};

export function useToasts() {
  return { toasts, dismiss, pause, resume, ...toast };
}

/**
 * A drop-in for a component's `error` / `message` ref: assigning a non-empty
 * string raises a toast, and the ref itself always reads empty so no inline
 * copy of the message is rendered.
 */
export function toastMessage(kind: ToastKind, title?: string): Ref<string> {
  return customRef<string>((track, trigger) => ({
    get() {
      track();
      return '';
    },
    set(value) {
      if (value) show(kind, String(value), title);
      trigger();
    },
  }));
}
