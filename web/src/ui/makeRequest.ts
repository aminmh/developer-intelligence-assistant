// Shared HTTP helper honoring VITE_API_URL for browser requests to the web
// server. It deliberately returns the native Response so callers retain their
// existing status, parsing, and error-handling behavior.
const apiBaseUrl = import.meta.env.VITE_API_URL || '';

export const makeRequest = async (path: string, options: RequestInit = {}) => {
  const base = apiBaseUrl.replace(/\/+$/, '');
  const url = base ? `${base}${path}` : path;
  return fetch(url, { credentials: 'include', ...options });
};
