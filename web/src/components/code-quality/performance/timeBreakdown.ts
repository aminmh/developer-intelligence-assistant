/** Average time per route from the benchmark's traced requests (see benchmark_traces in the API). */
export type TimeBreakdown = {
  route: string;
  traced: number;
  server_ms: number;
  server_p95_ms: number;
  database_ms: number;
  cache_ms: number;
  external_ms: number;
  app_ms: number;
  app_cpu_ms: number | null;
  client_ms: number | null;
  network_queue_ms: number | null;
};

export type BreakdownSegment = { key: string; label: string; ms: number; share: number; color: string };

const PARTS: { key: keyof TimeBreakdown; label: string; color: string }[] = [
  { key: 'database_ms', label: 'Database', color: 'bg-sky-400' },
  { key: 'cache_ms', label: 'Cache', color: 'bg-teal-400' },
  { key: 'external_ms', label: 'Outbound calls', color: 'bg-violet-400' },
  { key: 'app_ms', label: 'Application code', color: 'bg-amber-400' },
  { key: 'network_queue_ms', label: 'Network & queueing', color: 'bg-slate-500' },
];

/** Segments of one route's mean response time; shares are of k6 latency when known, otherwise of server time. */
export const breakdownSegments = (row: TimeBreakdown): BreakdownSegment[] => {
  const total = row.client_ms ?? row.server_ms;
  return PARTS.flatMap(({ key, label, color }) => {
    const value = row[key];
    if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return [];
    return [{ key, label, ms: value, share: total > 0 ? Math.min(1, value / total) : 0, color }];
  });
};

export const breakdownLegend = PARTS.map(({ key, label, color }) => ({ key, label, color }));
