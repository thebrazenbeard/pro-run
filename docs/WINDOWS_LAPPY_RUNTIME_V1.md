# Windows Lappy Runtime V1

Status: host-specific runtime qualification record and reproducible operating profile.

This document records the first persistent Pro-Run deployment qualified on the Lappy Windows host. It is evidence about that exact host binding, not a claim that every Windows installation or model backend is qualified.

## Bound source and model

- Pro-Run source: `thebrazenbeard/pro-run@0abf6045d8e195557cebe9a37b86f2a1eaa9bbbe`.
- Model source: `rodrigomt/Qwen3.5-4B-Uncensored-Aggressive@d61dd146c8fd44c9a49cdb7f59f34e17b61902d8`.
- Model endpoint: loopback-only `http://127.0.0.1:18081/v1`.
- Advertised model ID: `qwen3.5-4b-local`.
- Runtime root: `C:\ProgramData\ProRun`.
- Persistent state: `C:\ProgramData\ProRun\state\state.db`.

The persistent host layout pins a Python 3.12 runtime and the model inference packages under the Pro-Run root instead of depending on a temporary workspace.

## Runtime topology

```text
Task Scheduler (SYSTEM)
  |
  +-- ProRun Qwen Endpoint
  |     -> start-qwen.ps1
  |     -> waits for >= configured free VRAM
  |     -> qwen_http.py
  |     -> 127.0.0.1:18081/v1
  |
  +-- ProRun Daemon
  |     -> start-prorun.ps1
  |     -> waits for /v1/models readiness
  |     -> python -m prorun ... daemon
  |
  +-- ProRun Watchdog
        -> every minute
        -> restarts either supervisor task when it is not Running
```

The long-running tasks use `SYSTEM`, highest run level, no execution-time limit, battery-safe settings, `IgnoreNew` duplicate suppression, and Task Scheduler restart settings. The watchdog is a second recovery layer because Task Scheduler restart-on-failure behavior alone was not sufficient in fault injection.

## GPU coexistence

The qualified host has a 4 GiB RTX 3050 Laptop GPU. The Qwen supervisor must not assume exclusive GPU ownership.

`start-qwen.ps1` checks free VRAM before model load. The initial Lappy binding uses a threshold of 3400 MiB. When another legitimate model-training/evaluation process owns the GPU, the Qwen supervisor remains alive and logs `WAIT_GPU` instead of repeatedly loading and crashing.

This was exercised while a separate H07/V2 trained-adapter evaluation occupied the GPU.

## Qualification evidence

The following behaviors were observed on the bound host:

1. The cached Qwen base model loaded locally under CUDA and returned the exact probe text `PRO_RUN_QWEN_READY`.
2. The loopback HTTP shim exposed `GET /v1/models` and `POST /v1/chat/completions`.
3. A direct durable Pro-Run run through that HTTP path completed with `PRO_RUN_HTTP_OK`.
4. A persistent installed runtime under `C:\ProgramData\ProRun` completed `INSTALLED_PRO_RUN_OK`.
5. The registered Task Scheduler path completed `STARTUP_TASK_RUNTIME_OK`.
6. Killing the Qwen supervisor task and invoking the watchdog restarted the supervisor.
7. While the GPU was intentionally occupied by another model evaluation, a submitted Pro-Run run remained durable through repeated connection failures.
8. That same run was not resubmitted. After GPU release, the Qwen supervisor observed sufficient VRAM, started the endpoint, and the existing Pro-Run run completed automatically with `GPU_CONTENTION_RECOVERED`.
9. The recovered event completed on its tenth attempt, demonstrating durable retry across a prolonged model-backend outage.

## Failures found during qualification

Qualification found and corrected several host/runtime assumptions:

- A temporary qualification venv was not acceptable as a persistent dependency; Python and inference packages were pinned beneath the Pro-Run root.
- PowerShell `$ErrorActionPreference = "Stop"` can turn harmless native stderr warnings into `NativeCommandError`; child processes are therefore launched with `Start-Process` and separate stdout/stderr files.
- Default scheduled-task configuration would have stopped long-running tasks after 72 hours and had no useful self-heal path; those settings were hardened.
- Task Scheduler restart-on-failure did not reliably recover the long-running wrapper in fault injection, so an explicit recurring watchdog was added.
- GPU contention with legitimate parallel model work can kill or prevent model startup on a 4 GiB device; the Qwen supervisor now waits for sufficient free VRAM.
- A completed run after backend recovery retained the previous transient provider error in `last_error`. That is a state-quality defect and should be fixed separately in the Pro-Run kernel.

## Current capability ceiling

The current Qwen HTTP shim is sufficient for text completion through Pro-Run's OpenAI-compatible adapter.

It does **not** yet implement OpenAI structured `tool_calls` generation from Qwen output. Therefore this host binding qualifies durable text-task execution and backend-outage recovery, but does not yet qualify model-originated tool invocation through this local Qwen shim.

The host is bound to the base Qwen revision listed above. A separately trained Vera/H07 adapter must not be substituted merely because its files exist; it requires its own completed behavioral qualification and an explicit runtime binding.

## Operational files

`ops/windows/` contains:

- `qwen_http.py` — loopback-only OpenAI-compatible text-completion shim;
- `start-qwen.ps1` — GPU-aware model supervisor;
- `start-prorun.ps1` — Pro-Run daemon supervisor;
- `watchdog.ps1` — supervisor task watchdog;
- `register-tasks.ps1` — scheduled-task registration.

The host also requires `runtime\model-path.txt`, containing the exact local model snapshot path. Keeping the model location as host configuration avoids silently baking a machine-specific cache path into source.
