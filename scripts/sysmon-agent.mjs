#!/usr/bin/env node
/**
 * sysmon-agent.mjs — Zero-dependency system monitor for the hybrid LLM setup.
 *
 * Samples host CPU/RAM/temp/GPU stats and serves the latest sample as JSON over
 * HTTP. The web app polls it through Docker Desktop's host.docker.internal
 * (web/server.ts proxies /api/sysmon → http://host.docker.internal:8765/stats).
 *
 * Requirements: Node 18+ (no npm dependencies). On macOS, install macmon for
 * temperature + GPU metrics (no sudo needed):
 *   brew install macmon
 *
 * Usage:
 *   node sysmon-agent.mjs                            # port 8765
 *   SYSMON_PORT=9999 node sysmon-agent.mjs
 *   SYSMON_BIND=127.0.0.1 node sysmon-agent.mjs      # keep off the LAN (no auth)
 *
 * temp/GPU policy:
 *   - macOS: macmon (brew install macmon) reports CPU/GPU temperature and GPU
 *     utilization WITHOUT sudo. If macmon is missing, the agent degrades to
 *     top/vm_stat for CPU/RAM and reports no temp/GPU data.
 *   - Linux: lm-sensors (`sensors -j`) supplies per-core temperatures when
 *     present; nvidia-smi supplies GPU utilization when present.
 *
 * macmon pinned at 0.8.2: the parser relies on its JSON keys (`cpu_usage_pct`,
 * `memory.ram_total`/`ram_usage`, `gpu_scaled_ratio`, `temp.cpu_temp_avg`/
 * `temp.gpu_temp_avg`) — upgrade macmon with caution.
 */

import { createServer } from 'node:http';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';

const PORT = Number(process.env.SYSMON_PORT || 8765);
const BIND = process.env.SYSMON_BIND || '0.0.0.0'; // bind to 127.0.0.1 to keep off the LAN
const SAMPLE_INTERVAL_MS = 2000; // refresh cached sample this often

// ---------------------------------------------------------------------------
// Subprocess helpers — every call is defensive; nothing here may crash us.
// ---------------------------------------------------------------------------

/** Run a command synchronously; return the spawnSync result or null. */
function runSync(cmd, args, timeoutMs) {
  try {
    return spawnSync(cmd, args, { encoding: 'utf8', timeout: timeoutMs });
  } catch {
    return null;
  }
}

/** Return stdout as a string when the command exited 0, else null. */
function readStdout(result) {
  if (!result) return null;
  if (result.status !== 0 || result.error) return null;
  return result.stdout ?? null;
}

function clamp(value, min, max) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return Math.min(Math.max(value, min), max);
}

// ---------------------------------------------------------------------------
// CPU (top on macOS; /proc/stat deltas on Linux)
// ---------------------------------------------------------------------------

function parseCpu() {
  if (process.platform === 'linux') return parseCpuLinux();

  const out = readStdout(runSync('top', ['-l', '1', '-n', '0'], 3000));
  if (out) {
    const m = out.match(/CPU usage:\s*([\d.]+)% user,\s*([\d.]+)% sys/);
    if (m) {
      const user = Number(m[1]);
      const sys = Number(m[2]);
      if (Number.isFinite(user) && Number.isFinite(sys)) {
        return clamp(user + sys, 0, 100); // user + sys ≈ total busy %
      }
    }
  }
  // Fallback: approximate from consecutive process.cpuUsage() deltas.
  return cpuFromProcessUsage();
}

/** Read cumulative CPU ticks from /proc/stat, or null when unavailable. */
function readCpuStat() {
  let out = null;
  try {
    out = fs.readFileSync('/proc/stat', 'utf8');
  } catch {
    return null;
  }
  const line = out.split('\n').find((l) => l.startsWith('cpu '));
  if (!line) return null;
  const fields = line.split(/\s+/).slice(1).map(Number);
  if (fields.length === 0 || fields.some((n) => !Number.isFinite(n))) return null;
  const total = fields.reduce((sum, n) => sum + n, 0);
  const idle = (fields[3] ?? 0) + (fields[4] ?? 0); // idle + iowait
  return { total, idle };
}

/** Linux: busy % from two /proc/stat reads ~100ms apart (GNU top rejects -l). */
function parseCpuLinux() {
  const first = readCpuStat();
  if (!first) return cpuFromProcessUsage();
  runSync('sleep', ['0.1'], 1000); // let the cumulative counters advance ~100ms
  const second = readCpuStat();
  if (!second) return cpuFromProcessUsage();

  const totalDelta = second.total - first.total;
  const idleDelta = second.idle - first.idle;
  if (totalDelta <= 0 || idleDelta < 0) return cpuFromProcessUsage();
  return clamp((1 - idleDelta / totalDelta) * 100, 0, 100);
}

let prevCpuUsage = null;
let prevCpuAt = 0;

// Last resort only: measures THIS process's CPU, not system CPU — a quiet
// agent reads ~0% even under full system load.
function cpuFromProcessUsage() {
  const now = process.cpuUsage();
  const at = Date.now();
  if (prevCpuUsage && prevCpuAt) {
    const busyUs = now.user + now.system - (prevCpuUsage.user + prevCpuUsage.system);
    const wallMs = Math.max(at - prevCpuAt, 1);
    const pct = (busyUs / 1000) / wallMs * 100; // % of one core, rough system proxy
    prevCpuUsage = now;
    prevCpuAt = at;
    return clamp(pct, 0, 100);
  }
  prevCpuUsage = now;
  prevCpuAt = at;
  return null; // first sample: no delta to measure yet
}

// ---------------------------------------------------------------------------
// RAM (macOS: sysctl hw.memsize + vm_stat; Linux: /proc/meminfo)
// ---------------------------------------------------------------------------

function parseRam() {
  return process.platform === 'linux' ? parseRamLinux() : parseRamMacos();
}

/** Linux: MemAvailable is what new work can actually use; used = total − available. */
function parseRamLinux() {
  let out = null;
  try {
    out = fs.readFileSync('/proc/meminfo', 'utf8');
  } catch {
    return { ramTotalBytes: 0, ramUsedBytes: 0, ramPercent: null };
  }
  const memTotalKb = readMeminfoKb(out, 'MemTotal');
  const memAvailableKb = readMeminfoKb(out, 'MemAvailable');
  if (memTotalKb == null || memAvailableKb == null || memTotalKb <= 0) {
    return { ramTotalBytes: 0, ramUsedBytes: 0, ramPercent: null };
  }
  const totalBytes = memTotalKb * 1024;
  const usedBytes = Math.max((memTotalKb - memAvailableKb) * 1024, 0);
  const percent = clamp((usedBytes / totalBytes) * 100, 0, 100);
  return { ramTotalBytes: totalBytes, ramUsedBytes: usedBytes, ramPercent: percent };
}

function readMeminfoKb(out, key) {
  const m = out.match(new RegExp(`^${key}:\\s+(\\d+)`));
  return m ? Number(m[1]) : null;
}

function parseRamMacos() {
  const totalStr = readStdout(runSync('sysctl', ['-n', 'hw.memsize'], 2000));
  const total = Number((totalStr ?? '').trim());
  const pageStr = readStdout(runSync('sysctl', ['-n', 'hw.pagesize'], 2000));
  const pageSize = Number((pageStr ?? '').trim()) || 4096;

  const out = readStdout(runSync('vm_stat', [], 2000));
  let usedBytes = 0;

  if (out) {
    const pages = (label) => {
      const m = out.match(new RegExp(`${label}:\\s+([\\d]+)`));
      return m ? Number(m[1]) : 0;
    };
    const reclaimableBytes = (pages('Pages free') + pages('Pages inactive') + pages('Pages purgeable')) * pageSize;
    usedBytes = Math.max(total - reclaimableBytes, 0);
  }

  // vm_stat unavailable → no used/percent can be derived; mirror the Linux
  // path, which also reports null percent on failure.
  const percent = out && Number.isFinite(total) && total > 0 ? clamp((usedBytes / total) * 100, 0, 100) : null;
  return {
    ramTotalBytes: Number.isFinite(total) ? total : 0,
    ramUsedBytes: usedBytes,
    ramPercent: percent,
  };
}

// ---------------------------------------------------------------------------
// Temperature (macOS: macmon, sudoless; Linux: lm-sensors per-core)
// ---------------------------------------------------------------------------

/** macOS: parse macmon single-sample JSON into coreTemps + aggregate tempC. */
function parseTempDarwin(macmon) {
  const temps = [];
  const { cpu_temp_avg, gpu_temp_avg, soc_temp_avg } = macmon.temp ?? {};
  if (Number.isFinite(cpu_temp_avg)) temps.push({ label: 'CPU', value: cpu_temp_avg });
  if (Number.isFinite(gpu_temp_avg)) temps.push({ label: 'GPU', value: gpu_temp_avg });
  // macmon 0.8.2 does not expose SOC temp; parse it defensively if ever present.
  if (Number.isFinite(soc_temp_avg)) temps.push({ label: 'SOC', value: soc_temp_avg });
  return { coreTemps: temps, tempC: temps[0]?.value ?? null, tempAvailable: temps.length > 0 };
}

/** Linux: lm-sensors per-core temps; tempC = max of per-core values (aggregate). */
function parseTempLinux() {
  const out = readStdout(runSync('sensors', ['-j'], 2000));
  const cores = [];

  if (out) {
    try {
      const data = JSON.parse(out);
      for (const [adapterName, adapter] of Object.entries(data)) {
        if (typeof adapter !== 'object' || adapter === null) continue;
        for (const [key, entry] of Object.entries(adapter)) {
          const m = key.match(/^Core (\d+)$/);
          if (m && typeof entry === 'object' && entry !== null && Number.isFinite(entry.temp1_input)) {
            cores.push({ adapter: adapterName, index: Number(m[1]), label: key, value: entry.temp1_input });
          }
        }
      }
    } catch {
      // Malformed sensors output — fall back below.
    }
  }

  if (cores.length === 0) {
    // No lm-sensors or no per-core entries → single CPU placeholder.
    return { coreTemps: [{ label: 'CPU', value: null }], tempC: null, tempAvailable: false };
  }

  cores.sort((a, b) => a.adapter.localeCompare(b.adapter) || a.index - b.index);

  // Multiple adapters each report "Core 0..N" — dedupe-free band charts would
  // collide, so disambiguate with the adapter name; single-adapter output stays
  // exactly as before ("Core 0", "Core 1", …).
  const multiAdapter = new Set(cores.map((c) => c.adapter)).size > 1;
  const coreTemps = cores.map(({ label, value, adapter }) => ({
    label: multiAdapter ? `${label} (${adapter})` : label,
    value,
  }));
  const tempC = Math.max(...cores.map((c) => c.value)); // max of per-core as aggregate
  return { coreTemps, tempC, tempAvailable: true };
}

// ---------------------------------------------------------------------------
// GPU (macOS: macmon scaled ratio; Linux: nvidia-smi)
// ---------------------------------------------------------------------------

/** Linux GPU instances: nvidia-smi utilization (works on Vast.ai boxes). */
function parseGpuLinux() {
  const out = readStdout(
    runSync('nvidia-smi', ['--query-gpu=utilization.gpu', '--format=csv,noheader,nounits'], 2000)
  );
  if (!out) return null;
  const m = out.match(/(\d+)/);
  if (!m) return null;
  return clamp(Number(m[1]), 0, 100);
}

// ---------------------------------------------------------------------------
// Sampling
// ---------------------------------------------------------------------------

/** macmon single sample: `macmon pipe -s 1 -i 250` prints one JSON object. */
function runMacmon() {
  const out = readStdout(runSync('macmon', ['pipe', '-s', '1', '-i', '250'], 2000));
  if (!out) return null;
  try {
    return JSON.parse(out);
  } catch {
    return null;
  }
}

/**
 * macOS sample: macmon is the primary source (sudoless, single JSON sample).
 * Only when macmon is missing do we degrade to top + vm_stat for CPU/RAM, so a
 * present macmon never blocks the event loop with extra ~1s subprocess calls.
 */
function sampleDarwin() {
  const macmon = runMacmon();

  if (!macmon) {
    return {
      cpuPercent: parseCpu(),
      ram: parseRam(),
      tempC: null,
      tempAvailable: false,
      gpuPercent: null,
      gpuAvailable: false,
      coreTemps: [],
    };
  }

  // macmon drives CPU/RAM; the top/vm_stat fallbacks cover the rare case where
  // macmon omits a field (not the per-tick path). cpu_usage_pct is a 0..1 ratio.
  const cpuPercent = clamp(macmon.cpu_usage_pct * 100, 0, 100) ?? parseCpu();

  const ramTotal = macmon.memory?.ram_total;
  const ramUsed = macmon.memory?.ram_usage;
  const ram =
    Number.isFinite(ramTotal) && ramTotal > 0 && Number.isFinite(ramUsed)
      ? {
          ramTotalBytes: ramTotal,
          ramUsedBytes: ramUsed,
          ramPercent: clamp((ramUsed / ramTotal) * 100, 0, 100),
        }
      : parseRam();

  const gpuPercent = clamp(macmon.gpu_scaled_ratio * 100, 0, 100); // 0..1 → %
  const temps = parseTempDarwin(macmon);

  return {
    cpuPercent,
    ram,
    tempC: temps.tempC,
    tempAvailable: temps.tempAvailable,
    gpuPercent,
    gpuAvailable: gpuPercent !== null, // only claim GPU data when a real value exists
    coreTemps: temps.coreTemps,
  };
}

/** Linux sample: top/vm_stat/nvidia-smi + lm-sensors per-core temps. */
function sampleLinux() {
  const ram = parseRam();
  const cpuPercent = parseCpu();
  const gpuPercent = parseGpuLinux();
  const temps = parseTempLinux();
  return {
    cpuPercent,
    ram,
    tempC: temps.tempC,
    tempAvailable: temps.tempAvailable,
    gpuPercent,
    gpuAvailable: gpuPercent !== null,
    coreTemps: temps.coreTemps,
  };
}

const HISTORY_CAP = 60; // matches the web monitor's 60-sample window

let lastSample = null;
const historySamples = [];

function sample() {
  const darwin = process.platform === 'darwin';
  const data = darwin ? sampleDarwin() : sampleLinux();

  lastSample = {
    host: os.hostname(),
    platform: process.platform,
    cpuPercent: data.cpuPercent,
    ramTotalBytes: data.ram.ramTotalBytes,
    ramUsedBytes: data.ram.ramUsedBytes,
    ramPercent: data.ram.ramPercent,
    tempC: data.tempC,
    tempAvailable: data.tempAvailable,
    gpuPercent: data.gpuPercent,
    gpuAvailable: data.gpuAvailable,
    coreTemps: data.coreTemps,
    cpuCores: os.cpus().length,
    timestamp: new Date().toISOString(),
  };
  historySamples.push(lastSample);
  if (historySamples.length > HISTORY_CAP) historySamples.shift();
  return lastSample;
}

// ---------------------------------------------------------------------------
// HTTP server — always serves the cached sample, never blocks on subprocesses.
// ---------------------------------------------------------------------------

function setCors(res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
}

function sendJson(res, obj) {
  res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(obj, null, 2));
}

const server = createServer((req, res) => {
  const method = req.method ?? 'GET';
  const path = (req.url ?? '/').split('?')[0];
  setCors(res);

  if (method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  if (method === 'GET' && path === '/stats') {
    const wantsHistory =
      new URL(req.url ?? '/stats', 'http://localhost').searchParams.get('history') === '1';
    const current = lastSample ?? sample();
    // history=1 sends the rolling buffer so a freshly loaded monitor can seed
    // its wave with real frames instead of ramping up from zero.
    sendJson(res, wantsHistory ? { ...current, history: historySamples } : current);
    return;
  }

  if (method === 'GET' && (path === '/' || path === '/health')) {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(`<!doctype html>
<html><head><meta charset="utf-8"><title>SysMon Agent</title></head>
<body style="font-family:system-ui,sans-serif;padding:2rem;max-width:40rem">
<h1>SysMon agent running</h1>
<p>GET <a href="/stats">/stats</a> — latest system sample as JSON.</p>
<p>Platform: ${process.platform} · Host: ${os.hostname()}</p>
</body></html>`);
    return;
  }

  res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
  res.end('Not found — try GET /stats');
});

// Listen on 0.0.0.0 (default) so Docker Desktop's host.docker.internal can reach
// us; set SYSMON_BIND=127.0.0.1 to keep the agent off the LAN.
server.listen(PORT, BIND, () => {
  console.log(`SysMon agent running → http://${BIND}:${PORT} (GET /stats), platform ${process.platform}`);
});

// Sample immediately, then refresh on a fixed cadence. A short startup burst
// (macmon needs ~1s/sample, so the burst runs at a 1s cadence) seeds the
// history buffer so the web monitor's wave has real frames from the first load.
sample();
let burstLeft = 4;
const burstTimer = setInterval(() => {
  sample();
  if (--burstLeft <= 0) clearInterval(burstTimer);
}, 1000);
setInterval(sample, SAMPLE_INTERVAL_MS);

process.on('SIGINT', () => {
  server.close(() => process.exit(0));
  process.exit(0);
});
