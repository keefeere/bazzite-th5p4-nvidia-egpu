# Controlled eGPU suspend diagnostics

This tool reproduces the investigated freezer/devices/platform PM tests. It
is not a suspend fix. Real tests have hard-hung this host; see
[the recorded outcomes](2026-09-18-suspend-status.md) before choosing another.
Use this repository copy for future work. Archived `worker.py` files under
`/var/log/egpu-sleep-lab/` remain the exact scripts used for historical runs.

From the repository root, `check` is read-only:

```bash
sudo python3 diagnostics/egpu-sleep-lab.py check --stage freezer
```

`run` explicitly requests one test through logind. Its default stage is freezer;
`--stage devices` and `--stage platform` must be selected explicitly. No full
s2idle wait loop, automatic retry or progression to deeper tests is implemented.
Partial NVIDIA `suspend_depth` values are not supported by this runner: it uses
`default` and restores `default` after a completed cycle.

The checks require a healthy NVIDIA device, kernel suspend notifiers, no active
compute clients except KWin, idle sleep services, no leftover diagnostic
overrides and `pm_test=none`. A root-owned transient system service owns the
test. Returning from the launch command only means the worker started.

The optional eGPU sleep guard blocks these requests too. Do not remove that
guard to run a diagnostic: the existing exception is narrowly limited to the
historical no-dock `host_reset=0` platform experiment. It requires:

- the exact three experiment kernel arguments and live `host_reset=N`;
- the updated guard's unit-scoped `ExecStartPre` installed by
  `install-egpu-sleep-guard.sh`;
- finalized recovery kargs with none of those experiment arguments, and no
  pending rpm-ostree transaction/staged deployment;
- `--stage platform --host-reset-guard-bypass` on both `check` and `run`.

Only after explicitly choosing to repeat that experiment, checking graphics,
and finalizing recovery with `egpu-host-reset-test.sh cancel`, the read-only
check is:

```bash
sudo python3 diagnostics/egpu-sleep-lab.py check --stage platform --host-reset-guard-bypass
```

Changing `check` to `run` requests the actual test. The root-only one-shot marker
contains the boot ID and stage. It is created after `pm_test=platform` is read
back, consumed before systemd-sleep, and removed if the request fails before
consumption. Other sleep services and manual status checks cannot use it.
No marker is created by `check`.

The runner temporarily enforces only `SuspendState=mem`, enables PM/debug
logging, applies existing SELinux labels, and saves the original settings.
It waits for an actual new sleep-service invocation to finish and requires
kernel evidence of every expected stage before reporting success.
NVIDIA, Cardwire, DisplayLink and the graphical session are otherwise unchanged.

After a completed or definitively refused cycle, overrides and PM settings are
restored. An uncertain request retains the selected test stage until inspection
or reboot; it never silently returns to ordinary full sleep while a request
may be pending. There is no automatic reboot, reset or second sleep request.

Logs are root-only under `/var/log/egpu-sleep-lab/<run>/`. Inspect the service
journal, `before.json`, `request.txt`, `cycle.json`, `result.txt`, `restored.txt`,
kernel events and any delayed stacks together. The observer freezes with other
userspace, so its timeout is not a watchdog and does not guarantee capture or
recovery. Missing kernel events after a hang do not identify the blocked callback.
Use `_SOURCE_MONOTONIC_TIMESTAMP` for timing, not journal reception timestamps.
Review logs for private data before publishing.

Run the hardware-free tests with:

```bash
for test in tests/test-*.py; do python3 -B "$test" || break; done
```

References: [kernel PM debugging](https://docs.kernel.org/power/basic-pm-debugging.html),
[NVIDIA 615 notifier implementation](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/kernel-open/nvidia/nv.c),
[OSTree deployment finalization](https://ostreedev.github.io/ostree/deployment/).
