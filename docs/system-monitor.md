# System monitor host agent

The chat section shows an animated System Monitor (CPU / RAM / GPU / temp)
polling the host agent. On macOS, install **macmon** once — temperature and GPU metrics then
work **without sudo** on Apple Silicon:

```bash
brew install macmon
node scripts/sysmon-agent.mjs
```

No `SYSMON_POWERMETRICS` env var anymore — macmon replaces the old sudo `powermetrics` path
entirely, so the agent runs fully sudoless on macOS. If macmon is missing, the agent degrades
gracefully to `top`/`vm_stat` for CPU/RAM and reports no temp/GPU data. On Linux it uses
lm-sensors (`sensors -j`) for per-core temperatures and `nvidia-smi` for GPU.

| Env var | Default | Purpose |
|---|---|---|
| `SYSMON_PORT` | `8765` | HTTP port the agent listens on. |
| `SYSMON_BIND` | `0.0.0.0` | Address the agent binds. |

`GET /stats` returns a JSON sample with `host`, `platform`, `cpuPercent`, `ramTotalBytes`,
`ramUsedBytes`, `ramPercent`, `tempC`, `tempAvailable`, `gpuPercent`, `gpuAvailable`,
`coreTemps` (array of `{label, value}` — e.g. CPU + GPU on macOS, per-core on Linux),
`cpuCores`, and `timestamp`.

**Security note.** The agent serves hostname + CPU/RAM/temp/GPU stats with no auth and
`Access-Control-Allow-Origin: *`, so any LAN client can read them while it binds `0.0.0.0`.
When the monitor is not needed from the LAN, run with `SYSMON_BIND=127.0.0.1`, or block port
`8765` in a firewall.

The web server proxies the agent at `/api/sysmon`, so the browser never needs direct access
to port `8765`. Run it under `nohup`/launchd so the monitor survives the chat session:

```bash
nohup node scripts/sysmon-agent.mjs > /tmp/sysmon-agent.log 2>&1 &
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `sysmon-agent.mjs` no temp/GPU data | macmon not installed (macOS) | `brew install macmon`, then restart the agent. On Linux, install lm-sensors (`sensors`) for temps; GPU needs `nvidia-smi`. Verify the `/api/sysmon` proxy path in the web app. |
