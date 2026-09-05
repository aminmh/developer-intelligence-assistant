// Tiny shared HTTP helper honoring VITE_API_URL. Used by the config cards
// (multiple consumers); components predating this module keep their own local
// copies to limit churn.
const apiBaseUrl = import.meta.env.VITE_API_URL || '';

export const makeRequest = async (path: string, options: RequestInit = {}) => {
  const base = apiBaseUrl.replace(/\/+$/, '');
  const url = base ? `${base}${path}` : path;
  return fetch(url, options);
};
