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

## Optional isolated kernel trace — validate without sleep first

The host has confirmed the PM events and NVIDIA function targets listed below.
The corrected collector passed a **no-sleep live probe** on Sep 19
(`trace-probe-fle4ygg2`): the marker was persisted and the private instance
removed. The subsequent 14:03 platform attempt hung: its persisted trace reached
userspace freeze entry after NVIDIA's prepare notifier, then capture stopped.
This does not identify where the host ultimately hung. The collector is not
installed by the eGPU installer and never requests sleep on its own. Inspect
capabilities without writing anything:

```bash
sudo python3 diagnostics/egpu_pm_trace.py check
```

The following separate probe creates a private tracefs instance, persists one
marker, and removes the instance. **It does not request sleep, change PM
policy, arm a guard exception, stop services, or log out:**

```bash
sudo python3 diagnostics/egpu_pm_trace.py probe
```

Inspect its printed log directory under `/var/log/egpu-sleep-lab/trace-probe-*`.
A clean result requires the marker in `kernel-trace.txt` and no capture/cleanup
errors in `kernel-trace-status.json`. Do not proceed to PM if this probe fails.
The first live probe (`trace-probe-1uwkc7gz`) persisted its marker but failed
cleanup validation with EBUSY. The user subsequently removed that exact retained
instance and completed the corrected probe above; do not delete other instances
by wildcard. The fix closes `trace_pipe` before switching the private tracer to
`nop`: Linux rejects that switch while the reader is open.

Only for a separately authorized lab test, `--kernel-trace` on the existing
lab `check`/`run` commands opts into collection. It does not select a deeper PM
stage or weaken guard checks; without the flag existing behavior is unchanged.
It is restricted to session-intact tests. The worker archives its exact helper
alongside `worker.py`; the probe does not arm a later test automatically.

The isolated instance records the four exact function **entries**
`pm_prepare_console`, `pm_notifier_call_chain_robust`, `nv_pm_notifier`, and
`nv_set_system_power_state`. It also records `power:suspend_resume`, device-PM
callback start/end, and `notifier:notifier_run`. These are not function-return
probes. Notifier events include unrelated callbacks: correlate the task/PID,
timestamp, and enclosing PM phase before drawing conclusions.

The buffer is 64 KiB per CPU, monotonic clock, with an 8 MiB streamed-file limit
and 180-second monotonic observation deadline. No global tracefs control,
dynamic probe, boot argument, RTC state or driver parameter is written.
The exact function filter must read back successfully before enabling tracing;
the start marker must be persisted before the lab can request PM. Capture
errors do not trigger retries or alter the PM outcome. Cleanup disables recording
and events only in the exclusively created instance, closes its reader before
switching its tracer to `nop`, records ring-overrun
statistics, and removes that exact instance without recursive deletion. If a
reader remains stuck, the disabled instance is retained and reported rather
than forcibly terminated. A SIGKILL/crash may leave an instance until reboot;
do not adopt or erase an unknown instance on a later run.

**Limitations:** the userspace reader can freeze along with other tasks. Its
deadline is not a watchdog. Buffered kernel events may be lost on forced reset;
there is no persistent-memory/pstore setup. Disk synchronization and tracing
add overhead and can change timing. Missing events, an empty trace, or a clean
capture shutdown do not establish which driver hung or that sleep succeeded.

A separate [reserved-RAM marker-retention probe](egpu-trace-retention.md) is
now observed to retain one marker across an ordinary reboot. Its initial
verifier false negative was caused by a changed callsite symbol label; the
payload and PID/CPU/time survived. It does not request PM, validate recovery
after a forced reset, change this collector or make the lab's
`--kernel-trace` mode persistent. Do not rerun the hanging platform test merely
because either no-sleep probe passed.

## Historical graphics-only roundtrip — no sleep

The Sep 18 19:02 no-graphics PM attempt recorded an NVIDIA flip timeout during
logout, before requesting suspend. The 20:11 graphics-only attempt recovered
the visible desktop but failed to reach quiescence: a user-bus stop error was
ignored, and recovery reopened SDDM before the old desktop finished stopping.
The runner now checks that request and waits for actual completion. Isolate
that corrected transition first:

```bash
sudo python3 diagnostics/egpu-sleep-lab.py check --graphics-only
```

After saving work and closing compute clients reported by preflight:

```bash
sudo python3 diagnostics/egpu-sleep-lab.py run --graphics-only --allow-logout
```

This stops and restores the same configured desktop/SDDM/Cardwire/DisplayLink
services as the no-graphics experiment below. **There is no sleep request, PM
setting write, suspend-depth change or sleep-guard exception.** A block sleep
inhibitor is held by the transient worker. NVIDIA modules, persistence and PCI
topology remain unchanged. `--stage` and all PM experiments are incompatible
with `--graphics-only`; `check` never ends the session.

Desktop shutdown uses the configured user's validated direct D-Bus socket,
with UID/GID dropped by `setpriv`, rather than the `--machine` transient bridge.
The stop request's exit status is mandatory. A shared 75-second polling
deadline covers shutdown and recovery, with separately bounded diagnostic
commands; this is not a fixed sleep. Both the desktop units (including KWin
and Plasma) and their queued jobs must settle, and GPU-client checks must
pass. A disappearing user bus alone is not evidence of completion. Unit
timeout/abort results are retained, including journal evidence after the user
manager ends naturally, and prevent progression into PM.

If old clients/jobs remain at the deadline, **the runner does not unmask or
restart the display stack**. It records the incomplete state for inspection;
graphics may stay unavailable until manual recovery or reboot. There is no
forced kill, repeated stop request, or fresh waiting budget during recovery.
Existing systemd unit stop-timeout policies are unchanged and may themselves
abort a stuck desktop service. Save work before this test.

A journal cursor captured before logout bounds new kernel messages, avoiding
false attribution of earlier boot errors. Losing the cursor, changing boots
or failing to read the journal aborts the diagnostic. New NVIDIA/AMD DRM
errors, flip timeouts and warnings make the outcome a failure even if service
restart succeeds. A nonfatal flip warning permits one attempt to return the
desktop, but never progression into sleep. Severe errors (Xid, NV_ERR_, GPU
fallen off the bus, kernel BUG/panic) prevent automatic GPU reopening. No
reset, forced module unload, retry or reboot is performed. Graphical recovery
is not guaranteed even without a PM test; normal logout closes applications.

Inspect `graphics-outcome.json`, `graphics-kernel-baseline.json`,
`graphics-kernel-{after-logout,before-restore,after-restore}.json`,
`graphics-errors-*.json`, `stop-desktop-targets.txt`,
`graphics-stop-progress.json`, `desktop-shutdown-errors.json` and the snapshots.
Final service state is verified,
but **the visible image still needs user confirmation**. The post-start
observation is bounded; later login problems must also be checked in the
journal. A failed run may still have `graphics-restored.txt` when services
returned but DRM errors were recorded; the outcome file distinguishes these.

The optional eGPU sleep guard blocks these requests too. Do not remove that
guard to run a diagnostic. There are three explicit, mutually exclusive,
platform-only experiments; neither permits ordinary sleep or hibernation.

## Historical host-reset experiment

The no-dock `host_reset=0` platform experiment requires:

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

## Cardwire-off experiment on a normal boot

This tests the absence of the Cardwire daemon, without changing driver options,
PCI resources, kernel arguments, DisplayLink or the graphical session. The
host must be on its normal `host_reset=Y` boot with no host-reset A/B arguments
and no staged recovery deployment. Do not combine it with the historical test.

After explicitly choosing this experiment, prepare it from the repository:

```bash
sudo systemctl mask --runtime --now cardwired.service
sudo ./install-egpu-sleep-guard.sh
sudo python3 diagnostics/egpu-sleep-lab.py check --stage platform --cardwire-off-guard-bypass
```

Only after saving work, changing `check` to `run` requests one potentially
hard-hanging **platform test**, not a full suspend. The preflight, worker and
guard require an exact runtime mask, an inactive service and no `cardwired`
process. The guard checks those conditions again just before systemd-sleep.
It consumes a boot-bound, root-only marker; retries, other sleep operations,
wrong stages and mixed host-reset experiments remain blocked. Ordinary menu
sleep remains blocked. The runner does not stop or start Cardwire itself.

Cardwire stays masked for the rest of this boot; reboot removes the runtime
mask and restores the usual enabled service. To undo it without reboot after
the diagnostic has completed (or before requesting it):

```bash
sudo systemctl unmask --runtime cardwired.service
sudo systemctl start cardwired.service
```

Do not restore Cardwire while a test is in flight. There is no automatic repeat
after a hang. Compare results with the normal-policy baseline, not as a strict
one-variable comparison against the earlier `host_reset=0` run. One successful
run would justify further confirmation, not establish causality by itself.

## No-graphics experiment (ends the desktop session)

This experiment also failed at 19:02 on Sep 18; do not repeat it before the
graphics-only roundtrip is understood. It keeps
NVIDIA modules, persistence, PCI resources and the physical enclosure in place,
but stops SDDM, Cardwire, DisplayLink and the configured user's graphical
session. It is not equivalent to logging out into the graphical SDDM greeter.
It never terminates an entire user manager or unrelated SSH/TTY sessions, and
refuses to proceed if another user's or a remote graphical session exists.

Preparation and a read-only check:

```bash
sudo ./install-egpu-sleep-guard.sh
sudo python3 diagnostics/egpu-sleep-lab.py check --stage platform --no-graphics
```

Close all NVIDIA compute/UVM clients reported by preflight (not only games:
Viber can also appear there). After saving all work, explicitly authorize logout
and one platform test:

```bash
sudo python3 diagnostics/egpu-sleep-lab.py run --stage platform --no-graphics --allow-logout
```

The root-owned transient worker survives logout. It saves original service
states, creates only runtime masks and requests ordinary desktop shutdown. If
GPU handles remain, **no sleep is requested**; it does not force-kill remaining
applications. Only the exact running `nvidia-persistenced.service` PID and
executable are allowed to retain GPU handles. The read-only checker checks all
visible `/dev/nvidia*` and `/dev/dri/*` handles, including the iGPU, and requires
all four NVIDIA modules to remain loaded. The sleep guard repeats that check
immediately before permitting the single diagnostic invocation.

The updated worker also checks **new** kernel GPU/DRM errors after logout,
before calling the PM experiment. Successful `nvidia-smi` and zero graphical
clients alone do not certify a clean modeset shutdown. The historical 19:02
worker did not have this extra gate; its archived copy is left unchanged.

The packaged DisplayLink sleep hook writes to FIFOs even when its daemon is
stopped. For this experiment only, a runtime `systemd-suspend.service` override
binds `/usr/bin/true` over that hook **inside the service's private mount
namespace**. The host's `/usr` file is not edited, and ordinary/Cardwire-only
tests keep the original hook. The guard refuses the no-graphics test unless it
sees the isolated hook. The namespace mechanism has been probed without sleep.

After a completed test or an aborted preparation, the worker restores only the
masks it created and the services that were previously active. SDDM returns in
its configured mode; terminated desktop applications and unsaved work are not
restored. An uncertain PM cycle, incomplete cleanup or recorded NVIDIA errors
blocks automatic reopening of GPU clients. There is no reset/reboot timer or
automatic retry. In a hard hang, only a reboot can clear the runtime masks and
overrides; a user-space watchdog cannot guarantee recovery.

Additional artifacts: `graphics-before.json`, `before-logout/`,
`graphics-quiescent.json`, `graphics-restored.txt` or
`graphics-recovery-required.txt`. The copied `egpu_no_graphics.py` beside the
archived worker is the exact orchestration code used for that run.

## Session-preserving backing-store A/B (615.71.09 only)

This is a **freezer/devices/platform diagnostic**, not a working suspend/resume solution.
The default remains `freezer`: NVIDIA PM notifiers execute, but the kernel's
device suspend phase is skipped. An explicit `--stage devices` also exercises
ordinary device suspend/resume callbacks, stopping before late/noirq/platform
handling and the real s2idle wait loop. Separately selected `--stage platform`
also exercises late/noirq and platform preparation, but returns at the kernel
test point before the s2idle wait loop. Any stage can hang. Save work before
an explicitly approved `run`. Do not repeat, compare variants, or advance
stages automatically; full sleep is not authorized by this path.

`--vram-backing current` keeps the host Btrfs `/var/tmp` as a baseline.
`--vram-backing private-tmpfs` gives only `systemd-suspend.service` a new
`/var/tmp` tmpfs through `TemporaryFileSystem=`. The host's mount/files,
NVIDIA module parameters, loader, desktop session, Cardwire, and DisplayLink
service configuration are not changed. The runner does not log out or restart
them, although their normal PM hooks/callbacks still execute. The private mount lasts for that service invocation;
the loaded driver continues to use the path `/var/tmp`. Its actual use of the
new filesystem by NVIDIA must be confirmed from evidence, not inferred solely
from the probe. Early samples record the sleep caller's mount namespace too.

Limits are intentionally narrow and checked again by the guard immediately
before systemd-sleep starts:

- Exactly one NVIDIA GPU, inspected driver **615.71.09**, at most 16 GiB total
  VRAM and at most **4096 MiB used**; no non-compositor compute clients in the
  lab preflight.
- At least **10 GiB MemAvailable** (swap is not counted); private capacity is
  **6 GiB**, with `noswap` and `huge=never`. Six GiB is a limit, not an upfront
  allocation. This is not suitable for an arbitrary fully loaded 16 GiB GPU.
- Normal host-reset boot, notifier/preservation enabled, `TemporaryFilePath`
  exactly `/var/tmp`, no staged recovery deployment or conflicting private
  mounts/sleep policy. Existing kernel and driver boot policies are untouched.
- The service-policy check accepts and records either the older NVIDIA package's
  exact `SYSTEMD_SLEEP_FREEZE_USER_SESSIONS=false` override or the newer
  `615.71.09-3` package's unmodified systemd default freezer policy. It rejects
  other explicit or duplicate values and never switches between these modes.
- Separate root-owned 0600 marker `/run/egpu-sleep-guard-freezer-once` (legacy
  filename retained), tied to boot ID, mode and the **exact requested stage**,
  expiring after 90 seconds. It is consumed
  before PM; unknown modes, other stages, stale markers, changed budgets and
  unverified mounts fail closed. A marker for one stage cannot authorize any
  other stage. None can authorize hibernation or normal sleep.

The inspected driver pre-reserves storage for used framebuffer memory before
GPU engine teardown, but this does **not** make a failed test harmless: earlier
NVIDIA preparation and concurrent application allocations remain possible.
The RAM check is a conservative admission test, not an OOM-proof reservation.

Preparation commands (none requests sleep):

```bash
sudo ./install-egpu-sleep-guard.sh
sudo python3 diagnostics/egpu_vram_backing.py probe
sudo python3 diagnostics/egpu-sleep-lab.py check --stage freezer --vram-backing private-tmpfs
# After reviewing the completed freezer result, preparing devices is read-only:
sudo python3 diagnostics/egpu-sleep-lab.py check --stage devices --vram-backing private-tmpfs
# After reviewing the devices result, platform preparation is also read-only:
sudo python3 diagnostics/egpu-sleep-lab.py check --stage platform --vram-backing private-tmpfs
```

`probe` creates a short-lived private service, validates the mount, writes and
reads one **1 MiB O_TMPFILE**, closes it and checks the host mount identity is
unchanged. It neither arms a bypass nor touches PM settings or GPU state. Its
private files and mount disappear when the service ends. `check` is read-only.

Only after separately agreeing to a potentially hanging test, run **one**:

```bash
sudo python3 diagnostics/egpu-sleep-lab.py run --stage freezer --vram-backing current
# OR, in a separately reviewed run with a healthy baseline:
sudo python3 diagnostics/egpu-sleep-lab.py run --stage freezer --vram-backing private-tmpfs
```

After a reviewed successful freezer result and **separate approval for the
next stage**, a single device-callback test uses:

```bash
sudo python3 diagnostics/egpu-sleep-lab.py run --stage devices --vram-backing private-tmpfs
```

After a reviewed successful devices result and **fresh explicit approval**, a
single late/noirq/platform test uses:

```bash
sudo python3 diagnostics/egpu-sleep-lab.py run --stage platform --vram-backing private-tmpfs
```

The V4 guard and matching installed helper are required. The default remains
`freezer`, even after successful deeper tests. Old archived workers with a
different helper are refused by the preflight. Earlier freezer/devices code
and successful hardware runs remain preserved in their respective log folders.
For `platform`, success additionally requires ordered late/noirq suspend and
noirq/early resume evidence, with no real s2idle loop entry. A partial device
roundtrip or a zero service exit alone is not enough. See the
[kernel suspend path](https://github.com/torvalds/linux/blob/master/kernel/power/suspend.c).

The script does not log out, restart the display manager, unload modules, or
retry. On confirmed return/refusal it restores the prior PM settings and
removes its runtime override. If completion is uncertain it revokes any unused
exception but keeps test settings for inspection rather than enabling real
sleep. Reboot clears runtime settings if needed; no automatic reboot occurs.
No diagnostic state is armed by installation or by `probe`/`check`, so those
steps alone need no rollback/reboot. Keep the ordinary sleep guard installed.

## Serial device-PM comparison (also hung on this host)

The private-tmpfs platform attempt at 00:56 on Sep 19 hung; the user had to
force a reboot. The retained journal does not identify the blocked callback.
Do not repeat that unchanged test or infer that private backing fixes suspend.

The subsequently authorized serial attempt at **05:40 on Sep 19 also hung**.
The service-side guard confirms serial/trace-off policy, adequate available
RAM and the private mount, so this was not a refused preflight. There is no
retained completion evidence. The supplied early stacks show child waiting,
then an empty kernel stack; neither identifies the fault.
This mode is preserved for reproducibility, **not a recommendation to retry**.
See [the recorded outcomes](2026-09-18-suspend-status.md).

`--serial-device-pm` adds **one variable** to that same platform/private-tmpfs
experiment: it temporarily changes `/sys/power/pm_async` from `1` to `0` so
the PM core handles device callbacks sequentially. This tests an ordering or
timing hypothesis; it does not serialize every driver-internal worker or
establish that the actual hang is in device callbacks rather than earlier
notifiers. The desktop session and NVIDIA/PCI configuration stay in place.

The [kernel's RTC-trace documentation](https://docs.kernel.org/power/s2ram.html)
notes that tracing also disables asynchronous device PM. Testing the scheduling
change separately avoids the clock changes made by RTC tracing. This mode
requires `pm_trace=0` and never writes to it or to the RTC. The standard
[PM async gate](https://github.com/torvalds/linux/blob/master/drivers/base/power/main.c)
is explanatory upstream source, not a claim that master is the host's exact
kernel source.

After installing the matching guard/helper, this preparation does **not**
request sleep, arm a bypass, or change PM settings:

```bash
sudo ./install-egpu-sleep-guard.sh
sudo python3 diagnostics/egpu-sleep-lab.py check --stage platform --vram-backing private-tmpfs --serial-device-pm
```

Only with a fresh readiness confirmation and all work saved, changing `check`
to `run` requests one potentially hard-hanging test. There is no reboot/logout
in the test itself, but a hard hang can still require manual recovery. The
flag is rejected with an implicit/default stage, `devices`, current/Btrfs
backing, other experiments, cleanup, or `--allow-logout`.

The launcher and copied worker require the unchanged `pm_async=1` baseline.
The exact serial/platform marker and the service-side guard require
`pm_async=0`, `pm_trace=0`, the same driver/memory budget and the isolated
private mount. A mismatched policy consumes the permission but refuses PM.
Successful stage evidence still requires the full ordered platform-test
markers; a finished unit or device-only return is insufficient.

On a confirmed completed/refused cycle or a preparation failure, the original
`pm_async` value is restored along with the other test settings. If completion
is uncertain, `pm_async=0` and `pm_test=platform` remain for inspection while
any unused bypass is revoked. Never request another sleep in that state.
Reboot clears these runtime settings; the runner never initiates one. Inspect
`before.json`, the guard's consumption message, PM snapshots, `result.txt` and
`restored.txt` together. There is no permanent serial-PM policy.

### Prepared RTC fingerprint path — not run

The reserved-RAM trace captured a fresh no-sleep generation but the next boot
rejected its ring-buffer metadata. It is not a reliable post-hang recorder on
this host. The opt-in `--rtc-pm-trace` mode uses the kernel's independent
[`pm_trace` RTC fingerprint](https://docs.kernel.org/admin-guide/pm/sleep-states.html)
instead. This identifies a *candidate* last suspend/resume event after a hard
reset; hashes can collide and the last event need not be the faulting driver.
It does not itself make suspend work.

The kernel also prints a decoded RTC "Magic number" on **ordinary boots with
`pm_trace=0`**. One inspected normal boot printed `10:762:87` and two
`AMDI0052:00` matches; earlier normal boots printed different hashes/matches.
The Sep 19 boot `dd998587-3489-451e-8e07-4ea17be3d921` independently printed
the same magic number and both matches while `pm_trace=0`; at 20:32 EEST,
NTP was synchronized and the UTC RTC agreed with wall-clock time to about one
second. This is a pre-test baseline, not a PM result.
Those lines are *not* evidence that an AMD device caused the suspend hang.
Only attribute a later hash to this experiment if that exact run's saved
`before.json` and service journal prove `pm_trace=1` was enabled and its
one-use guard was consumed before the PM request. The kernel source reads RTC
time and prints decoded matches during boot regardless of this run's intent,
and explicitly notes hash collisions.

This option is accepted only with `--stage platform --vram-backing private-tmpfs
--serial-device-pm`, without `--kernel-trace`. Serial device PM is already the
comparison baseline, so the new experimental variable is the RTC fingerprint.
The host must have `CONFIG_PM_TRACE_RTC=y`, an unchanged `pm_trace=0` baseline,
the matching installed guard/helper and all ordinary resource/session checks.
The service-side one-use marker requires `pm_async=0` and `pm_trace=1` exactly;
any mismatch consumes the exception without requesting PM. A completed or
definitively refused cycle restores both sysfs settings; an uncertain cycle
leaves the test stage and trace setting in place for inspection until reboot.
There is no automatic retry, logout, sleep, reboot or package change during
preflight. On Sep 19 the updated sleep guard/helper was installed, and the
read-only RTC preflight passed on boot `dd998587-3489-451e-8e07-4ea17be3d921`
with NVIDIA 615.71.09 and 2687 MiB VRAM in use. The repo and installed helper
hashes matched; afterward `pm_trace=0`, `pm_async=1`, `pm_test=none`, and no
sleep-guard exception was armed. **No RTC-enabled PM attempt has been run.**

After the user approved one attempt, a fresh preflight at 20:35 EEST refused it:
`MemAvailable=9853968 kB`, below the required 10 GiB (10485760 kB).
The RTX was using 3659 MiB of VRAM, still within its separate 4 GiB cap.
No sleep request, RTC write, or one-shot exception occurred; `pm_trace=0`,
`pm_async=1`, and `pm_test=none` remained unchanged. Do not lower the memory
admission threshold to force this test through.

RTC tracing overwrites RTC clock data. The displayed wall clock may be wrong
after a reset until network time resynchronizes; disabling `pm_trace` does not
undo that RTC side effect. After time synchronization, verify the hardware RTC
separately before assuming it has been repaired. A platform attempt has already hard-hung more than
once, so an actual invocation needs a separate explicit decision, saved work,
and a plan for forced-reset recovery. Read-only preparation after installing
the matching helper would be:

```bash
sudo python3 diagnostics/egpu-sleep-lab.py check --stage platform \
  --vram-backing private-tmpfs --serial-device-pm --rtc-pm-trace
```

Do not change `check` to `run` merely because preflight passes. After a hard
reset, inspect the new boot's kernel journal for PM trace hash matches before
any additional test, and confirm `pm_trace=0` and wall-clock/NTP state. Do not
attribute a hash to the experiment unless the run's saved settings and service
journal establish that `pm_trace=1` was active before the PM request. The
kernel documents RTC clock modification and that this mechanism also disables
asynchronous device PM; it is not a substitute for proving S2 resume.

## Execution and evidence

The runner temporarily enforces only `SuspendState=mem`, enables PM/debug
logging, applies existing SELinux labels, and saves the original settings.
It waits for an actual new sleep-service invocation to finish and requires
kernel evidence of every expected stage before reporting success.
The ordinary and Cardwire-only runner paths leave NVIDIA, DisplayLink and the
graphical session unchanged. The no-graphics path performs only the explicit
stops/restoration described above. Snapshots include Cardwire's service state,
the live kernel arguments and host-reset value.

After a completed or definitively refused cycle, overrides and PM settings are
restored. An uncertain request retains the selected test stage until inspection
or reboot; it never silently returns to ordinary full sleep while a request
may be pending. There is no automatic reboot, reset or second sleep request.

Logs are root-only under `/var/log/egpu-sleep-lab/<run>/`. Inspect the service
journal, `before.json`, `request.txt`, `cycle.json`, `result.txt`, `restored.txt`,
kernel events and any delayed stacks together. The observer freezes with other
userspace, so its timeout is not a watchdog and does not guarantee capture or
recovery. Missing kernel events after a hang do not identify the blocked callback.
The observer now first samples only `systemd-sleep` at nominal 2 and 6 seconds
(`stacks-early-1.txt`, `stacks-early-2.txt`). Broader samples start at nominal
15 and 45 seconds, with the PM caller first. Each task's evidence is flushed
and fsynced before the next task is read, so a later stalled read does not keep
all earlier stacks in a Python buffer. Small early samples do not query the
GPU, systemd or journal, run sysrq, or globally sync filesystems. Sampling and
file I/O can still perturb timing or block; none of this guarantees evidence
across a hard reset. Captures carry monotonic timestamps; absence of the
process or an empty kernel stack is explicitly inconclusive.
The collector now records selected per-thread `status` fields (including
`State` and context-switch counts), `wchan`, and `schedstat` (CPU execution
time, run-queue wait time, scheduling slices), before trying each kernel stack.
Each observation has its own timestamp and is persisted separately. No task
is stopped or ptraced, and no process memory, environment or command-line
arguments are collected by these additions. Missing context files do not
prevent reading the remaining fields or stack. These are non-atomic samples;
`wchan=0` and zero counters must not be interpreted as successful PM or proof
of a spin. In particular, the x86 ORC unwinder can refuse a running remote
task's stack. Additional reads/fsyncs may change timing; userspace can still
freeze before any useful capture. Historical workers/logs are unchanged.
Use `_SOURCE_MONOTONIC_TIMESTAMP` for timing, not journal reception timestamps.
Review logs for private data before publishing.

Run the hardware-free tests with:

```bash
for test in tests/test-*.py; do python3 -B "$test" || break; done
```

References: [kernel PM debugging](https://docs.kernel.org/power/basic-pm-debugging.html),
[NVIDIA 615 notifier implementation](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/kernel-open/nvidia/nv.c),
[OSTree deployment finalization](https://ostreedev.github.io/ostree/deployment/).
