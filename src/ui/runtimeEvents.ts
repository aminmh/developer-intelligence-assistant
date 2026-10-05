/** Subscribe to backend invalidations; each view fetches its own authorized data. */
export function subscribeRuntimeUpdates(callback: () => void): () => void {
  const base = (import.meta.env.VITE_API_URL || '').replace(/\/+$/, '');
  const source = new EventSource(`${base}/api/runtime/events`, { withCredentials: true });
  source.addEventListener('runtime.updated', callback);
  return () => source.close();
}
