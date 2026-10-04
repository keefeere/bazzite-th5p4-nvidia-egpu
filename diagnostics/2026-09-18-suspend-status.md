# Suspend investigation status — 2026-09-18

Host: ASUS ROG Xbox Ally X, AMD Radeon 890M, TH5P4, RTX 5070 Ti;
optional HP Thunderbolt Dock G4. Kernel 7.2.4-ogc3.1.fc44.x86_64,
NVIDIA open 615.71.09. Host sleep mode is **s2idle**, not ACPI S2/S3.

## Outcomes and their limits

| Experiment | Observed result | What it establishes |
| --- | --- | --- |
| Earlier boot without eGPU | One completed suspend/resume | The host can complete at least one transition without the enclosure |
| `pm_test=freezer`, NVIDIA depth `default` | Two completed runs on Sep 17 | NVIDIA preparation/freezer can succeed; intermittent failure is not excluded |
| `pm_test=devices` | Completed on Sep 17, including NVIDIA, TH5P4 and HP | Ordinary device callbacks can succeed; no proof of late/noirq/full sleep |
| `platform` with Steam Fossilize active | Aborted freezer, UVM task stacks | Active shader processing contaminated this run; it did not reach platform callbacks |
| Clean `platform` attempt | Hard hang; early `NV_ERR_NO_MEMORY` in `_memdescAllocInternal` retained | Early NVIDIA PM/allocation handling is a leading suspect, not a localized stack-trace diagnosis |
| Omit `RMDisableNoncontigAlloc=1` for one boot | Same user-visible hard hang | Omitting the Gamescope allocation policy alone is not sufficient |
| `host_reset=0`, HP attached | PCI validation stopped before NVIDIA loaded | No sleep conclusion can be drawn from this attempt |
| `host_reset=0`, no HP, existing resources only | Boot succeeded; platform test hard-hung | HP and local reserve/reallocation are not required for this observed hang; host-reset workaround is insufficient |
| Normal `host_reset=Y`, Cardwire stopped/runtime-masked | Platform test at 17:46 hard-hung | The running Cardwire daemon is not required to reproduce the hang; its absence alone is insufficient |
| Normal `host_reset=Y`, no graphics clients, SDDM/Cardwire/DisplayLink stopped | Test at 19:02 did not return; TTY switching remained possible | Active graphics clients are not required at the sleep request, but an NVIDIA DRM error during logout contaminated the baseline |
| Graphics-only logout/restart, no PM request | Test at 20:11 returned a usable desktop, but shutdown failed and recorded flip timeouts | Display recovery is possible; this was not a clean quiescent roundtrip or a successful suspend test |
| Corrected graphics-only runner, 20:47 | Confirmed quiet GPU before restarting SDDM; desktop returned; fresh flip timeouts remain | The sequencing defect is fixed in this run, but the DRM baseline is still not clean |
| Ordinary Plasma logout/login, 23:21, no diagnostic or PM | Desktop returned; same NVIDIA flip timeouts | The warning is not exclusive to our runner and does not by itself establish the sleep hang's cause |
| Sep 19 00:09, private-tmpfs freezer test, session intact | Freezer stage confirmed, NVIDIA responds, same KWin/session; no new NVIDIA error retained | One bounded private-store preparation/freezer roundtrip succeeded |
| Sep 19 00:46, private-tmpfs devices test, session intact | Device stage confirmed; same KWin/session, full verifier clean, user confirms recovery | One ordinary-device roundtrip succeeded; this did not establish platform or real-s2idle recovery |
| Sep 19 00:56, private-tmpfs platform attempt, session intact | No completion; user confirms forced reboot | Private backing store did not prevent this hang; the last reached PM callback is still unknown |
| Sep 19 05:40, private-tmpfs platform with `pm_async=0`, session intact | Guard verified serial PM and adequate RAM; user reports hang; next inspection is on a new boot | Device-PM serialization is not a sufficient workaround; retained journal still does not locate the hang |
| Sep 19 14:04, same serial/private-tmpfs platform test with kernel tracing | User reports hang; trace reaches process-freeze entry after the NVIDIA notifier; display-flip errors precede that notifier | This invocation progressed past console preparation and NVIDIA's prepare notifier; the terminal PM stage is not retained, and a freezer deadlock is not established |
| Sep 19 23:28, paired NVIDIA procfs PM, private tmpfs and serial platform test | Procfs `suspend` completed, then the platform test timed out; user saw the internal panel off and forced a reboot | Moving NVIDIA's full restore after kernel device resume did not produce a completed cycle; one VT-wait sample does not identify the terminal hang |

The evidence and remaining uncertainty were reported upstream as
[NVIDIA/open-gpu-kernel-modules#1376](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/1376).
The public issue contains selected journal lines, not unreviewed full logs or
diagnostic archives.

The host-reset attempt was at 16:41 on Sep 18. The retained service journal shows the
one-shot guard being consumed, DisplayLink's pre-hook returning successfully,
and systemd-sleep logging `Performing sleep operation 'suspend'...`. The user
reported a cursor on the internal display, slight RTX fan activity and a hard
hang requiring power-cycle. No completion record was retained in that journal.
The accessible kernel journal does not contain enough of the transition to
identify the stalled stage.

The protected files in `20260918-164140-knn8kdma` were subsequently inspected.
The boot ID matches `32555d5d-7294-40a8-98b7-9488ce4c53c5`. `request.txt` records
`exit=0` (request accepted, not sleep completed). There are no delayed stack
files, after-snapshot, cycle-completion or result files. Before the request,
VRAM usage was 2075 MiB and MemAvailable was 12968392 kB. Cardwire,
DisplayLink Manager, Steam and KWin were running; no `fossilize_replay` process
appears in the saved process list. These are baseline observations, not a
stack trace at the failure. Physical absence of HP did not stop DisplayLink's
daemon.

The subsequent `20260918-174614-u8118ozz` run belongs to boot
`84856830-6a4b-46c6-b81c-ab9a67d1dd3b`. Its saved Cardwire state is `inactive`,
`masked-runtime`, `MainPID=0`; no daemon appears in the process snapshot. The
guard rechecked and consumed the Cardwire-off exception. DisplayLink's pre-hook
finished. This time the kernel retained `PM: suspend entry (s2idle)` and
`Filesystems sync: 0.040 seconds`, then two AMD `DMUB HPD RX IRQ callback`
messages. Those messages also occur during normal running; they do not identify
AMD as the culprit. No freezer/test-point/completion messages, delayed stacks,
after-snapshot or new NVRM/Xid error survived. The pstore archive was empty.
The user again reported a hard hang and rebooted.

The no-graphics run `20260918-190217-dhv0ntpa` belongs to boot
`25a5914d-bf7f-4acc-b1cc-8251efc66d07`. Cardwire, DisplayLink and SDDM stopped at
19:02:20–21; the user session was removed. Both the saved checker output and
the guard's recheck list only the exact `nvidia-persistenced` PID as a GPU
client. The pre-PM NVIDIA query still succeeded: P8, 2 MiB VRAM. All four
NVIDIA modules remained loaded, as intended.

However, at 19:02:25, **before suspend**, NVIDIA logged `Flip event timeout on
head 0`, followed by a WARNING in `__nv_drm_handle_flip_event`. The same type
of error also occurred earlier in that boot (18:26 and 18:28), so it is not
proof of the suspend hang's cause. The old preflight checked clients/NVML but
did not reject new DRM warnings during logout; the archived worker preserves
that limitation. The updated no-graphics runner now has a journal-cursor-based
gate that refuses to progress into PM after new GPU/DRM errors.

At 19:02:40 the guard consumed its exception, the isolated DisplayLink hook
returned, and the kernel logged `PM: suspend entry (s2idle)`. `request.txt`
contains exit=0, but there are no after/delayed snapshots, completion, result
or graphics-restoration records. No subsequent kernel messages were retained;
pstore was empty. The user could switch VTs with Ctrl+Alt+F keys, saw a login
prompt only on tty2, but could not type/log in or reboot normally. Partial
kernel console responsiveness does not establish userspace recovery or the
precise stalled PM phase. The user forcibly rebooted. On the next boot RTX
responded, the services were active, pm_test was none, no one-shot marker or
lab overrides remained, and the sleep guard again blocked the enclosure.

The graphics-only run `20260918-201129-927k9i3m` belongs to boot
`ee5e6a90-672b-4a60-9cd9-71cc3972e874`. The user confirmed that the desktop
returned. However, its saved outcome is failed, with `sleep_test_called=false`.
The desktop stop request through `systemctl --user --machine=keefeere@.host`
returned transport errors. The old runner recorded but ignored that exit code,
then continued stopping SDDM. NVIDIA logged a flip timeout at 20:11:37.
Old KWin/Plasma clients remained after the short polling window; recovery
restarted SDDM at 20:11:53 while their shutdown jobs were still pending.

The old plasmashell stop job timed out at 20:12:28; the old KWin job then
timed out at 20:12:45. Systemd aborted those processes according to their
existing stop-failure policy. These were not forced kills by the diagnostic.
A further NVIDIA flip timeout occurred at 20:12:26. There was no PM request
or new suspend-entry message, no retained Xid/NV_ERR, and the current GPU and
services respond normally. This exposes a runner sequencing defect and a DRM
shutdown problem without invoking suspend; it does not identify the original
suspend hang's cause.

The corrected runner uses the validated user's direct D-Bus socket from a
separate credential-dropped process, checks the stop request's exit status,
and polls both desktop unit/job state and GPU clients. Stop and recovery share
one 75-second polling deadline; individual diagnostic commands also have
timeouts. It returns as soon as quiescent, not after a fixed delay. It does not
reopen SDDM while old clients/jobs remain, and retains unit timeout/abort
evidence even if the user bus has ended. Failed shutdown or fresh DRM errors
still prohibit a PM test.
The corrected read-only preflight passed on this same boot: direct user-bus
queries resolved all four active desktop units with no queued jobs, and NVIDIA
responded with `pm_test=none`. No session or system configuration was changed.
All 147 Python tests and the kernel-compatibility, NVIDIA-policy and PCI-library
shell tests passed. These checks do not establish hardware transition success.

The corrected graphics-only run `20260918-204714-j8m7i2y4` subsequently completed
the session transition on that same boot. The user-bus stop request succeeded;
Plasma stopped at 20:47:22 and KWin at 20:47:23, without their prior stop-job
timeouts. Discord's shutdown did time out and systemd killed that process. The
user manager finished at 20:47:48, with only NVIDIA persistence holding GPU
nodes, before SDDM was restarted at 20:47:53. The shared shutdown deadline still
had about 44 seconds remaining. NVIDIA flip warnings during logout and login
made the diagnostic report failure despite successful display recovery.
`sleep_test_called=false`; this was not a suspend attempt.

The user then performed an ordinary Plasma logout/login at 23:21, with no lab
worker or PM request. NVIDIA again reported flip timeouts at 23:21:28 and
23:21:48; the new desktop worked. No Xid/NV_ERR was retained in that transition.
This separates the diagnostic runner's fixed sequencing bug from a warning
that also happens during normal session transitions. It does not exonerate
the display path, but repeating logouts is no longer the leading next experiment.

## Newly inspected pre-freezer backing-store evidence

The saved `stacks-1.txt` from Sep 17's `20260917-110438-tc0wzbi0` contains a
`systemd-sleep` stack through:

```text
nv_pm_notifier -> nv_suspend_devices -> nvidia_suspend
  -> memmgrSavePowerMgmtState_KERNEL -> fbsrCopyMemoryMemDesc_GM107
  -> os_write_file -> kernel_write -> btrfs_do_write_iter
  -> folio_alloc_noprof -> __alloc_pages_direct_compact -> migrate_pages
```

Kernel source timestamps put about 32.4 seconds between suspend entry and
starting the freezer. Freezing then failed on 13 Fossilize/UVM tasks and the
system returned. This shows a slow NVIDIA VRAM preservation path through Btrfs
and direct memory compaction in **a run that returned**, not a captured cause
of any subsequent hard hang. It is not evidence that Btrfs is broken or that
the GPU exhausted VRAM.

The live driver's `TemporaryFilePath` is `/var/tmp`, on Btrfs. NVIDIA's source
opens an unnamed temporary file on that configured path; its README requires
sufficient backing-store space and recommends full GPU VRAM plus 5% as a
conservative capacity. The host has 22 GiB RAM, roughly 10–11 GiB available
at inspection, a 16 GiB GPU, and only 4.6 GiB capacity in `/run`.
Blindly switching to `/run` or allocating a large RAM filesystem is not a safe
general fix. A service-private alternative filesystem is now prepared for a
bounded A/B as described below; its actual use by NVIDIA is not yet verified.

The lab now adds small, PM-caller-only samples at nominal 2 and 6 seconds;
broader captures remain at 15 and 45 seconds. It prioritizes `systemd-sleep`
and flushes/fsyncs each task's evidence instead of retaining all stacks in
memory until the entire sweep ends. Early probes do not query NVML, systemd
or journald, trigger sysrq, or call global sync. They do not change PM policy.
The sampler can still freeze or block, and disk writes can perturb timing;
missing or empty stacks are never treated as success or a stage diagnosis.
The updated repository passes all 154 Python tests and all three shell suites.
No new sleep, logout, module reload, backing-store override or guard bypass
was performed while making this evidence-collection change.

### Backing-store preparation later on Sep 18 — no PM invocation

Inspection of NVIDIA 615.71.09 `RmReserveFbsrTempFile` shows a file reservation
for used framebuffer memory in the PM call path, before GPU engine teardown.
This supports testing a service-private path without reloading the module,
but does not prove a successful PM transition or safe failure of every earlier
preparation phase.

The lab now supports `--vram-backing current` and `--vram-backing private-tmpfs`,
strictly at `pm_test=freezer`. The latter creates a 6 GiB tmpfs only in the
sleep service, with `huge=never,noswap`. It does not alter the host `/var/tmp`,
module parameters, PCI resources, display session, or Cardwire. Admission
requires the inspected driver, <=4 GiB used VRAM, >=10 GiB MemAvailable,
no other compute clients, normal finalized next-boot policy, and the ordinary
sleep guard. A distinct boot-bound, root-owned, 90-second one-shot marker is
consumed by the guard before PM; changed conditions fail closed.

The independent no-PM probe succeeded in transient service
`egpu-vram-probe-796c7cd0bbc44bf3a7c5178a19c2bf4e.service`: exact private tmpfs
capacity 6442450944 bytes, `noswap`, successful 1 MiB unnamed-file
fallocate/write/readback. The host `/var/tmp` mount identity remained unchanged
and the service/mount ended. Linux omits `huge=never` from mount options when
it is the default; validation permits its absence but rejects another huge
mode or the global `[force]` override. This probe did not call NVIDIA or PM.

The updated diagnostic guard/checker were installed; the ordinary connected
enclosure remains blocked from sleep. Read-only lab checks then refused twice
because MemAvailable was below 10 GiB; adjacent measurements fluctuate around
that threshold. No guard exception or runtime sleep override was created,
`pm_test` remains `none`, and no sleep/logout/reboot was requested. Applications
were not closed to force the check through. The full hardware preflight is
therefore not yet confirmed. All 177 Python tests and three shell suites pass.
See `egpu-sleep-lab.md` for preparation, limits, and explicit-run commands.

On the Sep 19 follow-up, available RAM was 11.48 GiB and NVIDIA reported
2634 MiB used VRAM. The complete read-only command
`check --stage freezer --vram-backing private-tmpfs` passed on the same boot
`ee5e6a90-672b-4a60-9cd9-71cc3972e874`, including installed guard/checker and
next-boot recovery checks. The earlier capacity refusal no longer applied at
that observation. No diagnostic units were running; the sleep service was
inactive with no temporary filesystem override, and `pm_test=none`.
No PM request or one-shot arming was performed. Actual freezer/VRAM behavior
still requires a separately approved run after the user saves work; admission
checks will run again immediately before that experiment.

### Sep 19 00:09 — one explicitly approved private-tmpfs freezer test

Run: `/var/log/egpu-sleep-lab/20260919-000949-f5ofr4wx`, invocation
`30c816ff688d45acbebaedd9e56102d9`, on the same boot. The user explicitly
confirmed readiness and authorized one run. No repeat or deeper test followed.

The guard consumed the one-shot exception at 00:09:52. Its admission snapshot
reported 1745 MiB used VRAM and 15801651200 bytes MemAvailable. It verified a
private 6 GiB tmpfs with `noswap`; the DisplayLink pre-hook also returned.
The actual `systemd-sleep` PID 522249 subsequently had its own mount namespace
and `/var/tmp` tmpfs recorded in `stacks-early-2.txt`. That sample's kernel
stack was empty: it proves the mount context, not a sampled NVIDIA file write.
The sleep service's reported memory peak was approximately 1.9 GiB, consistent
with memory-backed preservation but not alone a direct attribution of all
allocations to NVIDIA.

Kernel **source** monotonic timestamps in `kernel-events.txt` show:

- suspend entry: 18101702703 microseconds;
- beginning userspace freeze: 18105076812 (3.374 seconds after entry);
- userspace freeze completed: 18105080175 (reported 0.003 seconds);
- the freezer debug wait was reached, followed by task restart;
- suspend exit: 18112754398 (11.052 seconds after entry).

Journal reception delayed several messages, so `kernel-test.txt`'s displayed
timestamps must not be used to infer notifier duration. `kernel-events.txt`
contains JSON records plus the diagnostic command's `[exit=0]` footer; parse
records line-by-line (e.g. `jq -R 'fromjson?'`), not as a single JSON document.

Both the sleep unit and worker finished successfully. `cycle.json`,
`result.txt`, and `restored.txt` confirm return and cleanup. `nvidia-smi -L`
responded afterwards. KWin PID **432482** appears in both before/after process
snapshots and still has its Sep 18 23:21 start time; login session **25** remains.
No NVRM/Xid/NV_ERR/flip-timeout/BUG/WARNING entry was found in the inspected
post-request kernel journal. The guard marker and lab overrides are absent,
`pm_test=none`, and the host `/var/tmp` remains on writable Btrfs.

This is one clean **freezer** result without logout, not proof that ordinary
s2idle suspend/resume is fixed. Earlier Btrfs-backed freezer tests also passed,
and memory usage differs from the historical failing runs. No causal claim
about Btrfs follows from this result. The user subsequently confirmed that the
session returned ("повернувся"). The next step is a separately prepared and
authorized device-callback test with the same private backing store.

The V2 backing-store exception now permits an explicitly requested `devices`
test as well as the unchanged default `freezer`. Its marker payload must match
the exact active `pm_test`; there is no automatic escalation, retry, platform
or full-sleep permission. The marker retains its legacy filename for cleanup
compatibility. Tests cover both directions of stage mismatch, invalid/full
sleep stages, and preserving the selected test stage while revoking permission
after an uncertain device transition. Preparation does not run the experiment.
The V2 checker/guard were installed and their source/installed hashes match.
All 184 Python tests and three shell suites pass. The complete read-only
`check --stage devices --vram-backing private-tmpfs` passed on the same boot,
with NVIDIA reporting 1607 MiB used VRAM. `pm_test` remains `none`, the sleep
service was inactive, and no temporary filesystem override was active at that
preflight. The separately authorized device-callback result follows.

### Sep 19 00:46 — one explicitly approved private-tmpfs devices test

Run: `/var/log/egpu-sleep-lab/20260919-004648-bj9awryr`, invocation
`93cc0657885a45db9a0f3112317587ad`, on the same boot. The user explicitly
authorized one `devices` run; no repeat or deeper test followed.

The guard consumed the devices-only exception at 00:46:51, with 1819 MiB used
VRAM and 15052439552 bytes MemAvailable. Both early samples confirm the actual
sleep process, PID 582290, had the private 6 GiB `noswap` tmpfs at `/var/tmp`.
The first stack was waiting for a subprocess and the second was empty; neither
directly samples the NVIDIA preservation write. The sleep unit reported about
1.8 GiB memory peak.

Kernel source monotonic timestamps confirm the requested stage:

- suspend entry: 20320363040 microseconds;
- userspace freezing began at 20323636909 (3.274 seconds after entry), and
  completed in 0.003 seconds; remaining freezable tasks took 0.001 seconds;
- device suspend completed in 390.074 milliseconds, followed by the five-second
  debug wait at 20324031902;
- device resume completed in 1489.007 milliseconds;
- suspend exit: 20333204445 (12.841 seconds after entry).

NVIDIA and Thunderbolt ordinary PCI suspend/resume callbacks returned zero.
No late/noirq callbacks were recorded, as expected for this stage. No
NVRM/Xid/NV_ERR, flip timeout, BUG or WARNING was found in the inspected
post-request kernel log. The observer's requested SysRq blocked-task dump
showed an I2C/BMI323 read and a framebuffer vblank wait during return; this
was a diagnostic snapshot, not a spontaneous warning or a demonstrated hang.

The worker and sleep unit both succeeded. `result.txt` confirms the devices
stage, and `restored.txt` confirms cleanup. The original login session **25**
and KWin PID **432482** remain. `nvidia-smi -L` responds; the full installed
verifier reports **0 failures, 0 warnings**, including Gen4 x4, HP Ethernet at
1000 Mb/s, and the USB display adapter at 5000 Mb/s. `pm_test=none`, the
one-shot marker and both runtime overrides are absent, and the host `/var/tmp`
is still writable Btrfs. The user subsequently confirmed that visible output
and input recovered normally ("все ок"). No further PM test was launched.

This demonstrates one device-callback return with the session intact, not
real s2idle sleep or a proven backing-store fix. An earlier Btrfs-backed
devices test also succeeded. A later-stage comparison still requires separate
preparation and explicit authorization; the V2 backing-store guard does not
permit `platform` or real sleep. Subsequent V3 preparation below extends only
the explicit diagnostic stage, not permission for an unrequested PM run.

### Sep 19 — preparing the private-tmpfs platform comparison

The V3 guard/helper accepts an explicitly selected `platform` backing-store
test in addition to freezer/devices. Default stage remains freezer. The same
root-owned, expiring, boot-bound marker must match the exact active test stage;
cross-stage use, ordinary sleep, old diagnostic markers and retries remain
forbidden. No kernel, PCI, NVIDIA loader or graphical-session policy changes
are needed. The private store's memory/driver admission limits are unchanged.

Tests cover exact platform consumption, all cross-stage mismatches, mixing
with legacy platform experiments, old guard/helper rejection, read-only check,
one-request cleanup, uncertain completion retaining `pm_test=platform`, and a
device-only return being insufficient for platform success. The upstream
`suspend_enter()` path checks `TEST_PLATFORM` after late/noirq preparation and
before `s2idle_loop()`. This is not a claim that those callbacks already ran
on this host with the new backing store. Actual PM execution still requires
fresh explicit user authorization.

All **194 Python tests** and the three kernel-compatibility/NVIDIA-policy/PCI
shell suites passed; shell syntax and `git diff --check` also passed. The V3
guard/checker were installed without a PM request. Installed and source helper
SHA-256 both read
`360606fd5d4587c37f1591fd7cfc07ebcd5c77f49612e0d11bd19c1075aa5eb4`.
The full read-only `check --stage platform --vram-backing private-tmpfs`
passed on the same boot, reporting 1603 MiB used VRAM. After preparation,
`pm_test=none`, the sleep unit was inactive with no private filesystem override,
both diagnostic markers and runtime override files were absent, and the
ordinary guard still blocked the connected enclosure. No platform run has
been launched with this backing store; next action requires user readiness
and explicit approval for that one potentially hanging test.

### Sep 19 00:56 — approved private-tmpfs platform attempt did not return

Run: `/var/log/egpu-sleep-lab/20260919-005656-mgqkthfg`, invocation
`b934adcfb6df4ce994ecf37640a708e5`, boot
`ee5e6a90-672b-4a60-9cd9-71cc3972e874`. The user's readiness confirmation
authorized one attempt. The guard consumed its exact platform permission at
00:56:59: 1778 MiB used VRAM, 14987087872 bytes MemAvailable, private 6 GiB
`noswap` tmpfs validated. DisplayLink's pre-hook returned; the kernel retained
`PM: suspend entry (s2idle)` and filesystem sync taking 0.024 seconds.

`request.txt` contains exit=0, meaning acceptance only. Both early stack files
survived. They verify the sleep process PID 603131 had the private `/var/tmp`
mount: the first was waiting for a subprocess, and the second, sampled at
monotonic 20931.592883, had an empty kernel stack. There is no full delayed
snapshot, after-snapshot, completion, result or restoration record. No new
NVRM/Xid/BUG/WARNING or freezer/late/noirq test-point record survived in the
post-request journal. Missing events cannot locate the hang, because journald
and the observer can freeze. An empty sampled stack is not proof of return.

The next boot began at 01:00:53, ID
`eaa2dc03-27f8-4a50-8205-ca7b9db036f4`. The user confirmed a forced reboot.
The old transient unit's current `inactive`, `MainPID=0`, `Result=success`
properties are not completion evidence for the previous boot. On the new boot,
the full installed verifier reports zero failures/warnings, RTX responds,
host_reset=Y, pm_test=none, and all diagnostic markers/overrides are absent.

The user's photograph shows only `sysrq: Show Blocked State`, not an error
diagnosis. Our observer requests that dump through SysRq `w`. The retained
instance is from the earlier successful devices run, at source monotonic
20332474623 microseconds / 00:47:05. The photographed timestamp appears to
match it, so a retained console message is a plausible explanation; the photo
does not establish a new platform-stage fault or identify a failing driver.
SysRq headers can be visible even when the console log level hides the actual
task dump (see the kernel SysRq documentation).

Both pstore directories were empty. Read-only inspection of this kernel's
config found `CONFIG_PSTORE=y` and `CONFIG_PSTORE_RAM=m`, but no
`CONFIG_PSTORE_CONSOLE` or `CONFIG_PSTORE_FTRACE`; EFI pstore is compiled with
default-disable. PM_TRACE_RTC and function tracing are built, but neither was
enabled here. Do not assume simply loading a pstore backend can preserve an
ordinary console trace on this build. No crash trigger, RTC trace, kernel
argument change, repeated suspend or new diagnostic configuration was applied.

The private-store hypothesis is insufficient as a standalone fix. Successful
freezer/devices runs followed by a failed platform attempt justify better
late-stage evidence, but do not prove that the failure occurred in a deeper
callback rather than an intermittent earlier notifier. Do not retry unchanged
or advance to real sleep based on the successful shallow tests.

Selecting `pm_test=platform` does **not** prove that execution reached a
platform callback. Preparation, NVIDIA notifiers, freezer, ordinary device
suspend and late/noirq handling precede its test point. A cursor and fan speed
cannot distinguish these paths. Neither impossibility of eGPU suspend nor a
specific NVIDIA/USB4/AMD culprit has been established.

### Sep 19 — preparation of the serial device-PM comparison

Initial read-only recheck on boot `eaa2dc03-27f8-4a50-8205-ca7b9db036f4`:
`pm_test=none`, `pm_async=1`, `pm_trace=0`; systemd-suspend is inactive with
MainPID=0 and no sleep-lab unit is loaded. This is an idle baseline, not a
live test waiting to complete. At that point the installed guard/helper were
V3 and the repository's V4 serial mode was not installed. `sudo -n true`
required a password, so the agent did not perform privileged installation.

The inspected kernel configuration provides PM_TRACE_RTC, but no pstore console
or ftrace capture. RTC tracing changes the hardware clock and also disables
asynchronous device PM. Rather than combine tracing and scheduling changes,
the next proposed comparison changes only `pm_async=1` to `0` relative to the
failed private-tmpfs/platform run. This is an ordering/timing hypothesis, not a
known workaround. If the hang is in an earlier NVIDIA notifier, serialization
of device callbacks may have no effect. Missing journal messages do not settle
which case applies.

The new `--serial-device-pm` mode is restricted to an explicit platform test
with private-tmpfs backing. It verifies a normal asynchronous/trace-off
baseline, archives the original policy, changes async scheduling only for the
one invocation, and rechecks serial/trace-off policy when consuming permission.
It restores the saved policy after completed/refused PM; an uncertain request
retains diagnostic settings and revokes permission without retry. No RTC,
kernel argument, driver policy, PCI topology, or graphical session is changed.
Hardware-free tests cover CLI restrictions, worker propagation, one-shot
consumption, policy changes, failed preparation, completion/refusal restoration,
and retaining settings after an uncertain cycle.

Validation: all 214 Python tests and all three shell test suites passed;
`bash -n` for the guard/installer and `git diff --check` passed. These checks
mock hardware/PM and are not evidence of a successful real suspend. No new
sleep, logout, reboot, driver reload, or installation was performed.

The user subsequently installed V4 and supplied a successful read-only serial
preflight on the same boot: NVIDIA 615.71.09, 1892 MiB used VRAM, selected mode
`vram-tmpfs-serial`, platform/private-tmpfs, no sleep requested. At 05:06 a
direct read-only inspection confirmed both installed files match the repository,
are root-owned regular files with mode 0755, and the helper SHA-256 is
`b89684bb322f73b4cb010333c5d46d4a2714411f24310eda0a3ba288daa63675`.
Both one-shot markers and both diagnostic runtime overrides are absent;
`PreparingForSleep=false`, `pm_test=none`, `pm_async=1`, `pm_trace=0`, and no
sleep-lab unit is loaded. A fresh agent-side `sudo -n ... check` was denied
for lack of authentication; it did not run or change host state.

Installation is no longer a blocker. The successful check is preparation,
not fresh authorization to risk another hard hang: user readiness and host
sudo authentication are still required before one run. Do not reinstall or
repeat the failed platform experiment merely because the goal auto-continues.
Preparing this diagnostic does not establish progress through platform PM or
successful full s2idle. The goal remains suspend/resume with the eGPU and the
same user session, not merely blocking sleep or passing this test.

The subsequent user-authorized serial launch authenticated through Visage but
was rejected by the launcher's read-only memory preflight: less than 10 GiB
MemAvailable. No transient worker or sleep request was created. Immediately
after refusal, MemAvailable was 10436468 kB (about 9.95 GiB); `pm_test=none`,
`pm_async=1`, `pm_trace=0`, and systemd-suspend remained inactive. This is a
resource-admission refusal, not another suspend failure. The memory limit was
not relaxed and no applications were stopped automatically.

### Sep 19 05:40 — serial/private-tmpfs platform attempt also hung

After the user's renewed readiness, exactly one run was launched:
`/var/log/egpu-sleep-lab/20260919-054035-x_s33aoh`, invocation
`51b579fe29eb40f2b2fd2bc0931822c1`, boot
`eaa2dc03-27f8-4a50-8205-ca7b9db036f4`. The copied worker's command includes
`--stage platform --vram-backing private-tmpfs --serial-device-pm`.
The launcher returning zero only confirms creation of the transient unit.

At 05:40:38.654878 the guard consumed the platform-only exception and verified
`pm_async=0`, `pm_trace=0`, 1982 MiB used VRAM, 14099890176 bytes MemAvailable,
and a private 6 GiB `noswap` tmpfs. The normal DisplayLink pre-hook completed
at 05:40:40.314081. The sleep process then requested suspend. Retained kernel
events end with `PM: suspend entry (s2idle)` and filesystem sync taking 0.032
seconds. No new NVRM/Xid/BUG/WARNING, freezer-completion, late/noirq or PM-exit
message was found after the request. The worker journal has an early-1 sample
announcement but no completion/result/restore message. The user reports a hang.

On inspection at 11:17 the host is on a new boot,
`7e883e92-9d1e-4f6d-8e4c-323d26bf2b91`, started at 10:20:19. The old transient
unit is not loaded, so its default `Result=success` is not a historical result.
RTX responds to `nvidia-smi -L`; `pm_test=none`, `pm_async=1`, `pm_trace=0`;
no sleep-lab unit is loaded and the ordinary eGPU guard blocks sleep.

Serial device PM plus private backing therefore is not a sufficient workaround
for this observed failure. This does not exclude driver-internal races or prove
which phase hung: missing journal events can reflect frozen userspace. In
particular, selecting a platform test still does not prove that its late/noirq
callbacks were reached. Do not repeat this unchanged test or deepen it.

Both noninteractive sudo and the subsequent fingerprint/password attempt failed;
the waiting read-only command was cancelled and its terminal handle exited.
The user subsequently supplied the directory listing and both early stack files:

- `early-1`: sample 16787.350398, PID/TID 437302 (`systemd-sleep`),
  `do_wait -> kernel_waitid -> __do_sys_waitid`. This is a wait for a child;
  it is consistent with the still-running normal sleep hook, which the journal
  subsequently records as successful. It is not an NVIDIA failure stack.
- `early-2`: sample 16791.351933, same task and mount namespace, empty kernel
  stack. This sample is after the recorded start of suspend, but does not
  identify the current instruction, task state, or the last reached PM phase.
- Both show `mnt:[4026533152]` and a 6 GiB `noswap` tmpfs at `/var/tmp`.
  This verifies the sleep caller's mount context, not actual NVIDIA file writes.
- The directory listing contains no after-snapshot, full delayed capture,
  cycle, completion, result or restoration file. It includes both early files,
  even though only the early-1 announcement survived in the journal.

Kernel source timestamps for suspend entry and filesystem sync are respectively
16789168215 and 16789200334 microseconds. Journal receipt timestamps differ by
about 0.668 seconds here. Keep the original timestamps and do not treat mixed
clocks as a precise measurement of callback duration.

The local kernel has `CONFIG_UNWINDER_ORC=y`, `CONFIG_SCHED_INFO=y`, and
`CONFIG_SCHEDSTATS=y`. Upstream x86 ORC deliberately refuses to unwind a task
executing on another CPU. That is one possible explanation for an empty stack,
not proof that this particular task was running, spinning or complete. No task
state/CPU counters were recorded in these historical early files.

The repository-only collector now records selected task `status` fields,
`wchan`, and `schedstat`, each timestamped and persisted before the stack read.
It never stops/ptraces the task, reads application memory, or requests a GPU
ioctl. Observations are explicitly non-atomic; zero/missing values remain
inconclusive. Missing context does not skip other fields or the stack. These
additional reads and file syncs can perturb timing and do not guarantee evidence
after freezing. The archived worker and all old evidence remain unchanged.
No second sleep, driver reset, logout or reboot was requested by the agent.
Validation after the collector change: 218 hardware-free Python tests and all
three shell suites passed; guard/installer shell syntax and `git diff --check`
also passed. No real PM run was used to validate this collector addition.

### Sep 19 — read-only NVIDIA integration and SELinux follow-up

The running module reports `UseKernelSuspendNotifiers=1`,
`PreserveVideoMemoryAllocations=1`, and `TemporaryFilePath="/var/tmp"`.
The legacy NVIDIA suspend/resume/hibernate services are all `LoadState=not-found`.
Thus the inspected service configuration does not show simultaneous legacy
service and kernel-notifier handling. Disabling notifiers alone would leave
the documented procfs integration missing; it is not a valid comparison.
The distro's `SYSTEMD_SLEEP_FREEZE_USER_SESSIONS=false` drop-in is present and
unchanged. Its VT-switch warning is another reason not to toggle it blindly.

NVIDIA's September 9 explanation identifies an unresolved 615.71.09 SELinux
problem with anonymous shmem backing, but explicitly distinguishes configured
filesystem paths. The exact public 615.71.09 source chooses `filp_open` when
`NVreg_TemporaryFilePath` is set; anonymous `shmem_file_setup` is the other
branch. Our explicit `/var/tmp`, including a private tmpfs mounted there,
does not select that anonymous-shmem branch. The linked patch was not applied
or verified against the installed binary, and is not an established fix here.

SELinux is enforcing and `nvidia-driver-selinux-0.1-2.fc44` is installed;
package presence does not prove the relevant operation is allowed. The retained
journal from 05:40:30 through 05:42 contains no matching AVC/SELinux denial.
The sudo PAM warning about `/run/user/0/bus` is not an NVIDIA backing-file AVC.
Root-only audit logs were not inspected, so SELinux is not ruled out.
No policy, driver, sleep integration, or runtime PM setting was changed and
no additional suspend was requested during this follow-up.

References:

- [NVIDIA explanation of 615 defaults and the shmem/SELinux issue](https://forums.developer.nvidia.com/t/corrupted-video-memory-and-crashes-with-nvidia-open-driver-for-debian/379365/10)
- [Exact 615.71.09 temporary-file allocation branches](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/kernel-open/nvidia/os-interface.c#L1859)

### Sep 19 — tracing capability survey, no tracing enabled

The running kernel remains 7.2.4-ogc3.1.fc44.x86_64. Its matching development
configuration in `/usr/src/kernels/7.2.4-ogc3.1.fc44.x86_64/.config` enables
function/function-graph tracing, kprobe events, and PM debugging. The matching
headers define `power:suspend_resume`, `power:device_pm_callback_start/end`,
and `notifier:notifier_run`. This last existing event is a candidate for
identifying entry into an individual PM notifier without adding a custom probe.
It records a callback pointer, not its return or PM event argument; it must be
correlated with the sleep task and PM phase, not interpreted alone as a hang.
`/proc/kallsyms` lists `nv_pm_notifier` and `nv_set_system_power_state` in the
loaded NVIDIA module, but that does not establish function-tracer eligibility.

Actual tracefs event availability, function eligibility, and existing tracing
ownership remain unverified: tracefs is root-only and noninteractive sudo
requires authentication. No tracefs instance, event, probe or filter was written.
Read these first before designing a bounded trace; do not enable global tracing
or overwrite another collector's settings. Ordinary in-memory traces may be lost
on forced reset. Persistent buffers need separate boot configuration and cannot
be assumed to survive a power cycle; no such configuration is authorized or
installed. This survey is not permission for another suspend attempt.

### Sep 19 — trace targets confirmed; optional collector prepared

The user supplied privileged read-only output confirming all four proposed
events in `available_events`, and the four exact functions in
`available_filter_functions` (also the NVIDIA `.part.0` clone). The global
tracer was `nop`, with `tracing_on=1`; that combination does not show whether
events were enabled. This resolves target availability, not the sleep fault.

`diagnostics/egpu_pm_trace.py` now provides read-only `check` and a separate
**no-sleep** `probe`: a unique private trace instance must persist a marker,
then is disabled and removed. The optional lab `--kernel-trace` archives the
helper and starts it before PM policy changes; failed startup or an observed
collector failure before the request prevents PM. Existing invocations without
the flag retain their behavior. Guard exceptions are revoked before trace
cleanup, and a trace cleanup error cannot skip ordinary PM-setting cleanup.

The collector restricts the function tracer to four exact entries, enables only
the selected events in its own instance, uses a monotonic clock and 64 KiB per
CPU buffer, and limits streamed output to 8 MiB/180 monotonic seconds. It does
not trace function returns. A frozen or disk-blocked reader still cannot
guarantee retention; this is not persistent tracing or a watchdog. Event
absence and capture success are not PM outcome evidence. See the lab guide for
cleanup/ownership checks and the preserved-instance failure case.

Validation: 240 hardware-free Python tests, all three shell suites,
guard/installer syntax, and `git diff --check` passed. This includes filter
rejection before activation, exclusive instance ownership, failed marker/sync,
short writes, size/deadline limits, private cleanup, stuck-reader retention,
read-only CLI checks, archived helper propagation, no sleep on capture-start
failure, and retaining uncertain PM state. Tests use fake tracefs and mocked PM.
The live probe and any traced PM cycle are **not yet run**; no runtime tracing,
driver change, logout, suspend, or reboot was performed in this preparation.

### Sep 19 — first no-sleep trace probe exposed a cleanup defect

The user ran `egpu_pm_trace.py probe` and supplied output for
`/var/log/egpu-sleep-lab/trace-probe-1uwkc7gz`. Capabilities passed, global tracer
was `nop`, global tracing was on, and global events were disabled. The start
marker was persisted, but final status validation failed. No sleep was requested.
The agent's noninteractive sudo cannot read this root-only run; the actual
`capture_error`/`cleanup_errors` and retained-instance state are still requested
from the user, not assumed from the traceback.

Code review independently found that `stop()` switched to `nop` while its
`trace_pipe` descriptor was still open. Upstream `tracing_set_tracer()` rejects
a tracer switch when `trace_ref` is nonzero with `EBUSY`; `tracing_open_pipe()`
increments that reference and `tracing_release_pipe()` decrements it. A new
regression test reproduced the bad order and failed with the modeled EBUSY.
Cleanup now stops recording/events, joins the reader, closes the pipe, and only
then switches to `nop` and removes its exact instance. A stalled reader retains
the disabled instance without attempting the forbidden switch. Failed probes
also print their status JSON and include the exact instance path in new reports.

The 14 collector tests and 114 lab tests pass after this fix; `git diff --check`
also passes. This is not yet confirmation of the historical probe's exact
error or of a successful corrected live probe. The old instance has not been
modified/removed, no new probe or PM cycle was requested by the agent, and no
historical logs were changed. Inspect the old run before retrying.

The user's subsequent status/config output confirms 306 persisted bytes,
`capture_error=null`, one cleanup error `[Errno 16] Device or resource busy`,
and `instance_retained=true`. The exact retained instance is
`/sys/kernel/tracing/instances/egpu-pm-4c9641b36eed47c38ca8fc2b23a4c6e4`.
This matches the independently reproduced open-pipe/tracer-switch defect.
Noninteractive root access is still unavailable to the agent. Removal of only
this temporary instance and the corrected no-sleep probe remain pending user
execution; the old log directory must be preserved. No successful probe or
suspend outcome is inferred from persistence of the start marker.

Source: [Linux trace-pipe lifetime and tracer-switch restriction](https://github.com/torvalds/linux/blob/master/kernel/trace/trace.c).

### Sep 19 — corrected no-sleep trace probe passed

The user supplied the successful result for
`/var/log/egpu-sleep-lab/trace-probe-fle4ygg2`. The command chained removal of
the exact old instance with `&&`, so the second probe's execution confirms
the old removal succeeded. The corrected probe persisted its marker and
reported successful removal of its own instance, with no capture/cleanup
error. Historical log directories remain intact. This validates basic live
trace startup, streaming and cleanup only, not PM event coverage or suspend.

Read-only host checks after this report: boot
`7e883e92-9d1e-4f6d-8e4c-323d26bf2b91`, `pm_test=none`, `pm_async=1`,
`pm_trace=0`, no loaded sleep-lab/suspend unit; NVIDIA 615.71.09 responds
with 3497 MiB used VRAM. Installed sleep guard and VRAM helper hashes match
the repository, so these helpers do not need reinstalling for tracing.
The complete privileged PM preflight remains pending; noninteractive sudo
still needs authentication. No new PM request was issued.

The proposed next diagnostic is one separately authorized platform/private-
tmpfs/serial invocation with `--kernel-trace` (the same PM settings as the
failed 05:40 run, now with kernel events and task-context sampling). It can
still hard-hang and lose evidence after userspace freezes. Read-only `check`
and fresh user readiness must precede it; a passed probe is not authorization
for PM, and no automatic retry or deeper stage is planned.

### Sep 19 — traced-platform preflight refused ordinary desktop GPU clients

The user ran the read-only platform/private-tmpfs/serial/trace check; it refused
two clients before making any PM request. Read-only process/cgroup inspection
identified PID 16286 (`gjs -m /app/bin/re.sonny.Junction`) as the Junction
Flatpak and PID 107981 as a Chrome GPU process in the YouTube Music PWA's
systemd app unit. Its desktop entry names YouTube Music. NVIDIA reporting these
as compute clients does not establish a heavy workload or fault in those apps.
VRAM was 1850 MiB and MemAvailable 15841416 KiB; these were not the blocking
thresholds. This lab's low-load diagnostic admits only KWin among compute
clients, so the earlier generic advice that all browsers could stay open was
too broad. Full success still requires normal session-preserving sleep, not
permanently requiring the user to close ordinary apps.

Only error formatting was changed: display the executable token, not the
basename of a command-line argument or complete browser flags. The message now
explains that desktop apps may hold such contexts without heavy GPU activity.
The client policy is unchanged; no new whitelist or guard bypass was added.
116 lab tests and `git diff --check` passed. No process was terminated by the
agent, and no traced PM cycle has started.

### Sep 19 14:04 — first traced platform attempt hung; new display-transition evidence

The user reported another hang. Read-only inspection confirms a new boot,
`df1a3a07-800e-4a4b-8c7c-bf379b9198fd`, started at 14:06:38. The previous
boot was `7e883e92-9d1e-4f6d-8e4c-323d26bf2b91`. Its journal identifies
the exact run as `/var/log/egpu-sleep-lab/20260919-140353-psvvxr4f`, unit
`egpu-sleep-lab-20260919-140353-psvvxr4f.service`, worker PID 320467,
with platform/private-tmpfs/serial-device-PM/kernel-trace arguments. This is
now an executed, user-reported failed PM attempt, not merely a passed check.

Retained journal evidence:

- The one-use guard accepted the private tmpfs namespace, 1719 MiB used VRAM,
  16917598208 bytes available RAM, `pm_async=0`, and `pm_trace=0`.
- `systemd-sleep` PID 321476 left user sessions unfrozen under the existing
  distribution policy. Its DisplayLink pre-hook completed successfully before
  it requested kernel suspend.
- At journal wall time 14:04:17.607189, `PM: suspend entry (s2idle)` appeared;
  the filesystem sync message reported 0.236 seconds.
- At 14:04:20.983190, NVIDIA DRM logged `Flip event timeout on head 0`.
  At 14:04:21.355318 it warned at `nvidia-drm-crtc.h:368`, in
  `__nv_drm_handle_flip_event`, on the `nvidia-modeset/` thread (PID 5185).
- KWin PID 15009 logged atomic-commit invalid-argument errors at 14:04:20,
  followed by permission-denied/DRM-lessee and output-configuration errors at
  14:04:21. These alone do not establish a SELinux denial; DRM/VT ownership
  transitions also need correlation.
- No freezer-completed, late/noirq-completed or PM-exit message was found in
  this interval. Their absence is not proof the corresponding stage never
  ran. Early observer messages at 14:04:16 and 14:04:20 are present; the
  underlying stack and trace files still need privileged reading.

This is a new retained display-transition symptom compared with the 05:40
serial attempt, not proof of a different root cause or an allocation failure.
Upstream 615.71.09 `nv_drm_atomic_commit()` waits up to three seconds for
pending display-flip completion and emits this timeout; its event handler
processes the flip queue. The online header's line numbering differs from
the installed warning location, so the exact warning condition must not be
asserted solely from that upstream header. `head 0` has not been mapped to a
physical connector. The trace must locate console/notifier/device-PM entry
before choosing another intervention; no new PM attempt or policy change is
justified by the timeout alone.

Source: [NVIDIA 615.71.09 display commit and flip-event handling](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/kernel-open/nvidia-drm/nvidia-drm-modeset.c).

Sep 19 follow-up: [NVIDIA issue #1361](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/1361)
reports the same 615.71.09 `Flip event timeout` plus warning at
`nvidia-drm-crtc.h:368` during compositor exit/suspend on other hardware.
The reporter also records **successful** suspend cycles with that warning.
Our Sep 19 14:04 failed PM run and a separate 16:16 graphical transition
show the same timeout/warning location. This strengthens the interpretation
that a display-transition regression is present here, but also shows the
warning alone is not sufficient evidence for the hard hang's root cause.
Issue #1361 is open and does not provide a validated fix for this eGPU stack;
no driver or display policy was changed on the basis of this comparison.
The later paired-procfs attempt instead retained a sync-FD semaphore error
only at the two-minute lab timeout, not this exact flip-timeout signature;
its VT-wait sample does not identify the terminal PM callback.

Post-reboot read-only checks show `pm_test=none`, `pm_async=1`, `pm_trace=0`,
no loaded lab/suspend unit, and the ordinary installed sleep-guard drop-in.
Noninteractive sudo still requires authentication, so no claim is made about
trace contents, byte count, completeness, cleanup files or the last recorded
function yet. The root-only run directory has not been modified. No sleep,
driver query/reset, logout, or service change was performed by the agent.

### Sep 19 — saved trace reaches the freezer; sampled VT wait was not terminal

The user supplied the root-only run's directory listing, trace configuration,
last 120 trace lines and both early stack files. `kernel-trace.txt` exists at
approximately 1.1 MiB. The listing has no final trace status/buffer statistics,
after-snapshot, cycle/result or restoration files. Neither complete collection
nor a particular cleanup failure can be inferred from those missing files.
The configured private instance was `egpu-pm-f83810e409a5449a9fc0d3822a8f4613`,
with monotonic start 13435.291502062, 64 KiB per CPU, 8 MiB/180-second limits.
This is below the byte limit, but without final status it does not prove why
the reader stopped. Historical tracefs instance state was lost across reboot.

Both samples target `systemd-sleep` PID/TID 321476 in namespace
`mnt:[4026532869]`, with the expected private 6 GiB `noswap` tmpfs on `/var/tmp`:

- At stack sample 13438.371215, the task was sleeping in `do_wait` /
  `kernel_waitid`, consistent with the subsequently completed pre-hook.
- At stack sample 13442.372105, the task was sleeping in
  `__vt_event_wait.part.0 -> vt_waitactive -> vt_move_to_console ->
  pm_prepare_console -> enter_state`. Independent nearby status/wchan samples
  agree about the VT wait, subject to their explicitly non-atomic collection.
- Later, the trace records the same PID at 13446.681699 in notifier event
  `bsp_pm_callback`, followed at 13446.681788 by
  `suspend_resume: freeze_processes[0] begin`.

Consequently the sampled console wait was not a permanent stop: execution
subsequently reached the start of process freezing. Upstream suspend ordering
places this tracepoint after console preparation and a successful return from
the PM notifier chain. The preceding individual NVIDIA entries are not in
the supplied tail, so their presence/timings must still be read from the full
file. This also does not establish successful GPU state preservation or rule
out downstream effects from the retained NVIDIA/KWin display errors.

The last supplied event is at 13446.683004, only about 1.2 ms after
`freeze_processes begin`. The tail is otherwise dominated by
`notifier_run: pvclock_gtod_notify [kvm]` approximately every millisecond.
Those are trace events, not error messages or evidence that KVM caused the
hang. The userspace collector can itself freeze at this boundary, so the
end of the file cannot distinguish a freezer failure from a later device or
platform failure. The next read-only action is to filter the existing full
trace for PM/function events and the suspend caller, not repeat PM or change
console/freezer/driver policy. Noninteractive root access remains unavailable;
no new test or runtime change was made.

Source: [Linux suspend preparation order](https://github.com/torvalds/linux/blob/master/kernel/power/suspend.c).

### Sep 19 — filtered full trace confirms progress beyond NVIDIA's prepare notifier

The user supplied the requested filtered events from the same saved trace.
All of the following run in `systemd-sleep` PID 321476 (timestamps are from
the same `mono` trace clock):

| Timestamp | Recorded event |
| --- | --- |
| 13440.029209 | `pm_prepare_console <-enter_state` |
| 13440.029574 | `evdi_painter_vt_notifier_call [evdi]` |
| 13443.897457 | `pm_notifier_call_chain_robust <-enter_state` |
| 13443.994206 | `nv_pm_notifier <-notifier_call_chain` |
| 13446.681699 | Next notifier: `bsp_pm_callback` |
| 13446.681788 | `freeze_processes[0] begin` |

The console-entry-to-notifier-chain interval is 3.868248 seconds. The interval
from the NVIDIA notifier entry to the next notifier is 2.687493 seconds;
this includes any scheduling/chain overhead and is not a function-return
measurement. The same caller advancing to the next notifier and then freezer
entry establishes that it did not remain stuck inside NVIDIA's preparation
notifier in this run. Upstream 615.71.09 returns `NOTIFY_BAD` on a failed
`nv_set_system_power_state()` call, otherwise `NOTIFY_OK`; reaching process
freezing is consistent with accepted preparation. It does not establish
correct saved VRAM, successful later device PM, or a healthy resume path.

The retained flip timeout/warning precede the NVIDIA notifier and overlap
the console-preparation interval. Their journal receipt/source clocks differ
from the trace clock, so no exact cross-clock duration is inferred. EVDI's
notifier appearing during VT preparation alone does not implicate DisplayLink.
The separate `nv_set_system_power_state` entry is absent from the supplied
filtered output. The previous capability listing included its `.part.0`
compiler clone, which the current exact four-function filter does not trace;
absence is not proof that the state-setting implementation never executed.

No device-callback events or freeze-end record appear in this filtered
persisted file. Since the trace reader is a userspace task, capture stopping
at freezer entry is expected to leave later phases unobservable after a hard
reset. This is not a measured freezer deadlock and does not justify disabling
freezing, VT switching, or NVIDIA notifiers. Another otherwise identical PM
attempt with the same retention mechanism is not the next diagnostic.

Read-only retention feasibility checks found `trace_instance=` and
`reserve_mem=` in the decompressed **installed** 7.2.4 kernel image, using
its supplied `extract-vmlinux` script. This is evidence the relevant parameter
strings are built in, not a validated persistent buffer or proof that this
firmware preserves RAM on reset. Configuration still lacks PSTORE_CONSOLE and
PSTORE_FTRACE. The existing tracing facility may permit a reserved-memory
buffer without a kernel rebuild. Preparing it would require explicit consent
for temporary boot parameters, same-kernel recovery, and a no-sleep marker
retention test across reboot first. Ordinary reboot success would not guarantee
retention after forced power-off; no arbitrary physical-memory address should
be selected. No parameters were staged, no trace instance was created and no
PM test was requested during this analysis.

Sources: [NVIDIA 615.71.09 PM notifier](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/kernel-open/nvidia/nv.c),
[Linux persistent trace-buffer prerequisites and limitations](https://docs.kernel.org/trace/debugging.html#persistent-buffers-across-boots).

## Sep 19: no-sleep reserved-RAM marker probe prepared, not activated

Following approval to prepare the retention experiment, added the standalone
`diagnostics/egpu_trace_retention.py` and its [procedure](egpu-trace-retention.md).
It does not reuse the ephemeral collector or alter production eGPU/PM behavior.
The proposed pair is `reserve_mem=16M:0x2000000:egpu_pmtrace` and
`trace_instance=egpu_pm_retention^traceoff@egpu_pmtrace`; no arbitrary physical
address and no KASLR disable. Recording starts off. Preparation alone changes
no live arguments, tracing or guard state.

The staged sequence is check, explicitly stage, ordinary reboot, mark, another
ordinary reboot into the same kernel/deployment, verify, and cancel. The probe
requires a changed boot ID, matching kernel-image hash and reserved mapping,
plus the exact unpredictable marker in the non-consuming live trace. Root-owned
ownership/evidence is retained; foreign pending deployments/arguments are not
overwritten. Interrupted staging without a recorded target checksum requires
inspection rather than automatically adopting the pending deployment.

All 290 hardware-free Python tests passed, including 47 new retention cases.
This validates mocked control flow, not real buffer allocation or persistence.
The privileged live `check` could not run because `sudo -n` required user
authentication. **No arguments were staged, no live marker was written, no sleep
or reboot was requested.** Even a later successful warm-reboot marker check
would not prove retention after a hard reset/power cut or establish a PM fix.

## Sep 19: marker survived warm reboot; symbol-dependent verifier false negative

The user subsequently ran check/stage, rebooted, wrote the marker, rebooted
normally again, and ran verify. The kernel image remained
`7.2.4-ogc3.1.fc44.x86_64`, SHA-256
`886f4e5f0cad0ea02649101cadfa21afee95a2e0357c684318c06a9ddba5b705`.
The verifier confirmed the same 16 MiB mapping at `0x644000000`, but initially
reported `marker_present=false`. A read-only kernel-journal check found the
previous-boot buffer metadata messages for all 16 CPUs.

The user's raw archives establish a false negative:

- Marker boot: `17c99098-a789-46ee-94fc-a045a6fa5b11`.
- Recovery boot: `43a59cbc-dc88-49c1-8bae-9c2c709d08a3`.
- Exact marker in both: `EGPU_RETENTION_2642e4eeac844ee0aaaffb8237c2d202_17c99098-a789-46ee-94fc-a045a6fa5b11`.
- Both headers: 1 entry buffered/written, 16 CPUs.
- Both records: PID 24306, CPU 013, timestamp `647.057509`.
- Original task/label: `python3`, `tracing_mark_write`.
- Recovered task/label: `<...>`, `kallsyms_offsets`.

The marker payload survived that ordinary reboot. The original `has_marker`
regex required the literal `tracing_mark_write` label and therefore rejected
the recovered record. Corrected parsing accepts exactly one structured trace
record with the exact marker payload, independently of the rendered symbol.
Verification additionally compares PID, CPU and timestamp with the protected
original archive, while still requiring the unchanged kernel/mapping and a
different boot ID. It reads the live trace, never substitutes a saved recovered
file, and preserves earlier verification results in state history. An existing
`not-retained` state can be rechecked without rewriting the marker or rebooting.

The symbol difference is not proof that `kallsyms_offsets` ran. Upstream
[trace_print_print](https://github.com/torvalds/linux/blob/master/kernel/trace/trace_output.c)
formats a saved callsite address separately from the text payload. The exact
reason the installed kernel's cross-boot address decoding produced this label
has not been established; future persistent PM/function tracing must account
for this before interpreting function names. One retained text marker proves
neither correct symbol decoding nor retention after a hang/forced reset.

Added regression coverage for the supplied record, changed symbols, malformed
or duplicate records, changed PID/CPU/time, archive-only false positives and
preservation of the previous false-negative result. No sleep, reboot,
boot-argument mutation or live trace write was requested while making this fix.
All 296 hardware-free Python tests then passed, including 53 retention tests.
The user subsequently ran the corrected privileged verifier in recovery boot
`43a59cbc-dc88-49c1-8bae-9c2c709d08a3`. It confirmed `same_mapping=true`,
`marker_present=true`, `same_record=true`, `symbol_changed=true`, and
`retained=true`. The mapping remained 16 MiB at `0x644000000`. This confirms
the ordinary-reboot marker result on the live buffer, not just saved archives.

### Persistent PM decoding preparation (no live recording)

The installed `7.2.4` kernel headers show that `suspend_resume.action` is a
`const char *` stored in the event. Device-PM callback start/end instead copy
device and driver strings into the record. Cross-boot decoding therefore needs
more than trusting rendered function/phase names. Upstream `trace_output.c`
also shows that raw/hex output can fall back to an event-type-only printer;
those switches must not be mistaken for a complete raw-buffer export.

Added a separate metadata-only helper, `diagnostics/egpu_persistent_trace.py`.
Its `check` is read-only; `bundle` saves bounded, root-private metadata and hashes
in a new diagnostic directory. It requires the current boot's verified live
marker, unchanged kernel/deployment/mapping, tracing off and the sleep guard
intact. It collects current symbol/module maps, event/header formats,
`last_boot_info`, per-CPU metadata and both marker records; it does not consume
or clear a trace, enable recording, modify the guard or invoke PM. No executable
PM recorder or sleep-lab integration has been added. See the
[procedure and limitations](egpu-trace-retention.md#decoding-metadata-preparation--still-no-sleep).

Read-only host checks found no pending deployment and no `trace-cmd` package.
Although `rpm-ostree install -yA` is the preferred additive-package path when
appropriate, installing it now would change the exact control deployment used
by the retention comparison; no package or deployment was changed. Binary export
and symbol decoding still need validation before another separately authorized
PM run. The new helper has hardware-free tests, but its privileged live metadata
collection has not yet been run. All 323 hardware-free Python tests passed,
including 27 new metadata tests. No sleep, reboot, trace-buffer write or power
transition was requested during this preparation.

### Sep 19 — live metadata bundle and offline saved-header decoding

The user ran `egpu_persistent_trace.py bundle` successfully in boot
`43a59cbc-dc88-49c1-8bae-9c2c709d08a3`, producing
`/var/log/egpu-sleep-lab/persistent-meta-s4cfbtus`. They then provided a private
local copy at the explicitly requested workspace path; original root-owned
evidence was not modified. All 49 manifest content hashes and the total of
26,589,771 bytes were verified. The map remained 16 MiB at `0x644000000`.

New evidence changes the decoder plan: `last_boot_info` contains only
`0\t[kernel]\n`. There is no nonzero old kernel base or list of prior module
addresses. CPU 13 statistics still show one marker entry, 108 bytes, with zero
overrun, commit overrun, dropped events and read events. Current kallsyms is
visible and saved `printk_formats` includes pointers for the PM phase strings.
The previous marker remains valid evidence of payload retention, but this
sample cannot drive automatic old-to-current address relocation. Upstream's
`update_last_data()` explains that initializing a new persistent recording also
sets kernel/module metadata and clears old records; this is a plausible reason
the marker-only path lacked relocation metadata, not proof of the exact build's
bug or permission to reset the instance.

Built trace-cmd 3.4, libtraceevent 1.9.0 and libtracefs 1.8.3 from pinned official
release commits under `/var/tmp/egpu-trace-tools.pV6jxKlz`, unprivileged. Local
library installation used a private prefix and `LDCONFIG=false`. No rpm-ostree,
host library, kernel or NVIDIA configuration was changed. Only version/help,
offline `restore` and `report -N` actions were run; no live extract/start/reset.

Added `diagnostics/egpu-trace-offline-check.py`. Actual offline execution with the
captured event formats and symbol table successfully decoded five **synthetic**
records (marker, NVIDIA function, `dpm_suspend_noirq`, device start and device
end with a deliberately injected -16 error). Those synthetic error/phase records
are NOT events from the host's failed sleep. A control run renaming the saved
symbol and PM string changed the decoded output accordingly, proving that this
path honors saved metadata. The successful result and reports are in
`/var/tmp/egpu-trace-offline-hztq73si`. See the
[offline procedure](egpu-trace-retention.md#offline-decoder-check-synthetic-records-only).

Next: a no-sleep test of actual recording/export from the owned reserved buffer,
archiving its old generation before initialization. The metadata must be captured
again when actual recording starts if module addresses or the boot have changed.
No live buffer modification, raw extraction, PM run or reboot was performed in
this follow-up. Normal sleep with the session intact is still unproven.
All 339 hardware-free Python tests passed (13 new offline checks and three
additional last-boot metadata cases). The booted deployment checksum remained
`8131aaaeb54a9d7aadc37450d70370801db79775cf7fa1f26832cf8f674ce052`, with no active
OSTree transaction or staged deployment and no active lab/suspend unit.

### Sep 19 — no-sleep recorder/export prepared, NOT executed

Following explicit authorization to prepare the next gate, added the separate
`egpu_persistent_probe.py` (`check`/`run`). It archives the old marker generation
before changing its owned RAM instance, records eight harmless `/dev/zero` reads with
PID/function/event filters, stops, and exports bounded raw CPU pages plus exact
same-boot metadata. It does not call PM, bypass the guard, alter PCI/driver state,
stop a session or change global tracing. Errors preserve partial files and leave
recording off; a tracer reset is allowed only after durable export. Probe phases
prevent reusing the old marker verification as if it still described live RAM.

The offline helper now has an explicit `--recorded` mode for a private copy of
that actual probe capture. It validates raw/metadata hashes and CPU ordering and
uses only saved-header restore/report, unprivileged. Synthetic and actual-capture
modes remain distinct. Neither is a PM or forced-reset test.

All 366 hardware-free Python tests passed, including 27 new probe/decoder tests.
They cover refusal gates, archive-before-write ordering, bounded
raw export, interruption/error cleanup, retained partial evidence, private file
modes, single-use state and saved-metadata decoding. An initial live read-only
preflight stopped because `__x64_sys_getpid` was absent from
`available_filter_functions`. The user confirmed that `vfs_read` is traceable;
the probe now pairs it with `syscalls:sys_enter_read` and eight `/dev/zero`
reads. The updated preflight then passed in boot
`43a59cbc-dc88-49c1-8bae-9c2c709d08a3` on the exact inspected kernel image
(`886f4e5f0cad0ea02649101cadfa21afee95a2e0357c684318c06a9ddba5b705`).
It made no trace writes. The live recording/export probe has **not** been run;
no real-capture success is claimed. No reboot,
sleep, tracefs mutation, host package/deployment change or production install was
performed during preparation. Next is the read-only probe check, then separately
authorized no-sleep execution and offline inspection — not a sleep retry.

## Recovery and guard

Staging cancellation with rpm-ostree was insufficient before a hard-hang test:
bootloader finalization normally occurs during orderly shutdown. The forced
reset booted the experimental deployment again. `cancel` now finalizes the
normal entry before testing and verifies there is no pending staged deployment
or experimental next-boot argument. The bootloader must still select that entry;
the historical experimental deployment may remain as a rollback choice.

The subsequent normal boot was checked: `host_reset=Y`, no test arguments,
healthy RTX at Gen4 x4, full verifier with zero failures/warnings, and the sleep
guard blocking the connected enclosure. The bypass marker was absent.

The optional guard blocks systemd suspend/hibernate paths while the enclosure
is connected. The lab exception is bound to one suspend invocation, exact boot
markers, boot ID and `pm_test=platform`; ordinary checks and hibernation cannot
consume it. This is containment, not a sleep fix.

## Remaining experiments, not executed

Sep 19 no-sleep retention gate: a fresh 18-record generation in the reserved
ftrace instance did **not** survive an ordinary same-kernel warm reboot. The
same 16 MiB region at `0x644000000` was mapped, but the next kernel reported
`Ring buffer boot meta mismatch of magic` and exposed an empty 0/0 buffer.
Both unique markers were absent; raw pages were not consumed. This supersedes
the earlier marker-only retention pass as a gate for using this configuration
after a forced reset. Do not spend another suspend hang relying on it. See
`egpu-trace-retention.md` for the preserved evidence and limits.

A separate opt-in RTC fingerprint mode has been prepared in the diagnostic
runner, but **not run**. The current kernel reports
`CONFIG_PM_TRACE_RTC=y`, while the live `pm_trace=0`, `pm_async=1` and
`pm_test=none`. The new mode requires the existing serial/private-tmpfs
platform test and checks `pm_trace=1` at the one-use service guard. It can
damage RTC wall-clock data and identify only a candidate last callback, not
automatically fix or localize the hang. See `egpu-sleep-lab.md`; no new PM
request follows from this preparation.
Read-only inspection also found that this normal boot printed an RTC "Magic
number" and `AMDI0052:00` hash matches while live `pm_trace=0`; earlier
normal boots had different matches. These ordinary boot-time lines must not be
attributed to the failed platform test. A future fingerprint is meaningful
only when the same run proves RTC tracing was enabled before PM.
The read-only installation audit found an active, unit-scoped sleep guard and
no pending OSTree transaction/deployment. On Sep 19, the updated guard/helper
was installed; the read-only RTC-mode preflight passed on boot
`dd998587-3489-451e-8e07-4ea17be3d921`. The installed helper matches the
repository version. Afterward `pm_trace=0`, `pm_async=1`, `pm_test=none`, and
the one-use guard exception was absent. No RTC write, suspend request, or
reboot was made. An actual RTC-enabled platform test still requires a separate
explicit decision because earlier platform attempts repeatedly hard-hung.

The latest trace establishes progress beyond console preparation and the
NVIDIA prepare notifier, but does not locate the final fault beyond process-
freeze entry. Before choosing another PM change, address capture retention
past userspace freezing. Both parallel and serial private-tmpfs platform
attempts have hung; neither should be repeated unchanged or treated as
permission for real sleep.
Consider better retention: early notifier sampling only works while userspace
can run; late/noirq failures may need persistent kernel logging or a suitable
external console. Larger journal quotas alone do not flush events while
journald is frozen. A kernel rebuild is not part of this plan.

## Sep 19 20:36 RTC platform attempt and two subsequent boots

The user approved one RTC-enabled platform diagnostic. After an earlier
low-memory refusal, the fresh preflight passed with roughly 13 GiB available
RAM and 1.8 GiB used VRAM. Run `20260919-203644-cesu8z8y` on boot
`dd998587-3489-451e-8e07-4ea17be3d921` armed `pm_test=platform`,
`pm_async=0`, and `pm_trace=1`. The systemd guard consumed exactly one exception
and verified the private 6 GiB tmpfs; the sleep request was accepted. The
kernel journal retained `PM: suspend entry (s2idle)` and filesystem sync,
but no completion. The user reported a hang and forcibly reset. Do not repeat
this configuration: private backing, serialized callbacks and RTC tracing did
not yield a successful transition.

The first post-reset boot (`034258f6-5f93-4267-a7c7-e11c1558fe39`) started
with a corrupted RTC date of Jul 25 before network time recovered. It decoded
`PM: Magic number: 0:142:402` and `hash matches drivers/base/power/main.c:1107`.
In the exact upstream 7.2.4 source, that line is `TRACE_RESUME(0)` immediately
after `TRACE_DEVICE(dev)` at the beginning of `device_resume()`. Decoding the
third field with the same source's seeded sdbm hash (`DEVSEED=7919`,
`DEVHASH=1009`) gives **402 for `0000:03:00.0`**, the RTX 5070 Ti endpoint
(`10de:2c05`) in the test boot. None of the other PCI BDFs visible in that
boot's kernel journal hash to 402. `device_resume()` has a second trace point
at line 1186 after the callback; the retained RTC value still points to its
entry at line 1107. This strongly localizes the last persisted trace to RTX
device resume, but cannot distinguish an NVIDIA callback from a subordinate
PCIe/TH5P4 stall or rule out a non-PCI hash collision or lost later RTC write.
The pre-test boot instead printed `10:762:87` and two `AMDI0052:00` matches
with `pm_trace=0`. The change in fingerprint, plus the proven `pm_trace=1`
guard consumption, makes the first post-reset result relevant to this attempt.

During that first boot, at 20:39:47 after user login, the kernel logged
`thermal_zone1: acpitz: critical temperature reached` and
`HARDWARE PROTECTION shutdown (Temperature too high)`. This explains the
user's unrequested second reboot, but the journal does not record the sensor
value at that instant. The user reported a cold chassis. In the next boot,
zone1 read 62 C (102 C critical trip) and CPU Tctl about 63 C; the report
does not establish sustained physical overheating or its cause. The current
boot (`e3a65f06-1f96-40f8-a9cf-363a1032c7c9`) has `pm_trace=0`,
`pm_async=1`, `pm_test=none`, NTP synchronized, but TH5P4 USB diagnostics are
present without its USB4/PCIe router; NVIDIA is not loaded. This is an
enclosure/router state after the failed transition, not evidence of a driver
installation failure. No repeat PM test, GPU reset or enclosure power-cycle
was initiated by this investigation.

### Source audit: a genuinely different NVIDIA resume ordering (not tested)

The 615.71.09 open kernel source shows that `nv_pmops_resume()` always calls
`nvidia_resume()`. In the current notifier mode, that PCI callback may do the
full `nv_power_management(..., RESUME)` and `nvidia_modeset_resume()`. The
documented `/proc/driver/nvidia/suspend` mode has a different sequence:
`suspend` first calls `nv_set_system_power_state()` and marks the GPU suspended;
the later PCI suspend callback increments `suspend_count` instead of repeating
the operation. During PCI resume, `nvidia_resume()` decrements that count and
returns without doing the full GPU restore. A **separate post-sleep `resume`
write** then calls `nv_set_system_power_state(RUNNING)` to do the full restore
after the kernel's device-resume pass. Thus a properly integrated procfs mode
could move the expensive RTX restore past the place identified by RTC tracing.
This is a testable hypothesis, not an established fix: PCI bridge handling,
GSP/firmware, and NVIDIA's later restore may still fail.

The host normally uses `UseKernelSuspendNotifiers=1`; no NVIDIA vendor
`suspend`/`resume` systemd services are installed. Merely turning the notifier
off would be unsafe. A reversible A/B was subsequently implemented with
paired pre-sleep/post-sleep hooks, a one-shot guard exception and automatic
return to ordinary notifier mode on the following boot. Its failed hardware
result is recorded in `diagnostics/egpu-procfs-pm-ab.md`. Do not repeat it
without new evidence or a changed variable.

1. **Early preservation evidence with the session intact.** The corrected
   graphics-only sequence has been tested, and ordinary logout also produces
   flip warnings. The private backing-store freezer and devices tests above
   now returned with the session intact. Their early samples establish the
   mount context, but did not catch a preservation write. Preserve these
   results before separately preparing any deeper test; low-load successes
   alone do not isolate the original failure.
2. **NVIDIA PM depth.** The 615 driver implements `modeset`, `uvm`, `default`.
   Its PM notifier uses the selected depth. Controlled **freezer-only** probes
   could distinguish display preparation, UVM/channel quiescence and full GPU
   preservation. Partial depths must never be paired with real device/platform
   sleep. The current runner deliberately accepts only `default`; extending
   it requires a separate change with matching guards.
3. **NVIDIA integration/backing-store path.** The old stack makes a controlled
   backing-store comparison worth preparing, while not proving causality.
   Validate namespace behavior and capacity without PM first. Alternatively,
   compare notifier integration with the documented procfs/systemd integration.
   Turning notifiers off without adding
   the corresponding suspend/resume handling is not a valid test. The local
   README says the open module preserves VRAM automatically; merely changing
   PreserveVideoMemoryAllocations is not necessarily an off switch.
4. **Transport versus driver.** If necessary compare Gen3 with Gen4, then a
   driver-free enclosure with its PCI functions still enumerated and unbound.
   This must not use the ordinary detach helper as a proxy: that also removes
   the PCI subtree, changing a second variable. A failure without NVIDIA
   shifts attention toward AMD/USB4/platform handling.
5. **Version comparison and hibernation.** A known previous deployment/driver
   can test regression, while noting any simultaneous kernel change. S4 may
   avoid parts of s2idle, but NVIDIA uses preparation/preservation there too;
   it is not an assured workaround for a pre-freezer failure. Validate actual
   resume storage/lockdown prerequisites before considering a controlled S4 test.

Low-priority toggles: the GPU reports video-memory self-refresh unsupported and
NVIDIA S0ix disabled despite EnableS0ixPowerManagement=1, so changing that value
alone is weakly motivated. Runtime D3 is already disabled/pinned by this stack.
Do not blindly relax D3cold/ASPM or rerun the same host-reset test without new
evidence. Every experiment above changes one condition and needs a fresh baseline.

## Sep 29/30 — Linux 7.2.7 and default user-session freezing still hang

Bazzite `44.20260928.1` moved the host from
`7.2.4-ogc3.1.fc44.x86_64` to `7.2.7-ogc1.1.fc44.x86_64`.  The NVIDIA
upstream version remained `615.71.09`; only the Fedora package revision moved
from `-1` to `-3`.  That package revision removed the former
`SYSTEMD_SLEEP_FREEZE_USER_SESSIONS=false` drop-ins.  Consequently an ordinary
systemd sleep now freezes `user.slice` instead of retaining the distro's legacy
Xorg-oriented no-freeze policy.

The backing-store diagnostic preflight was updated to recognize exactly two
known packaging states: `legacy-explicit-no-freeze` or the new
`systemd-default-freeze`.  It still rejects other explicit or duplicate freezer
values and never changes the host policy.  The installed guard/checker was
refreshed; the current-Btrfs platform preflight reported
`systemd-default-freeze`, the standard NVIDIA kernel-notifier transport, 148 MiB
VRAM in use, and a single platform-only exception.

Run `20260929-223533-fr_h4xih` consumed that exact exception.  The retained
service journal proves the new path was exercised:

- systemd successfully froze `user.slice`;
- the DisplayLink pre-sleep hook returned successfully;
- the procfs A/B hook correctly remained inactive;
- `systemd-sleep` began the suspend operation;
- the kernel's last retained PM record was `PM: suspend entry (s2idle)` at
  22:35:59.

There was no suspend completion, resume record, worker completion, or cleanup
record before the user forcibly rebooted the hung host.  Thus normal user-session
freezing on Linux 7.2.7 does **not** fix this eGPU platform transition.  It also
makes an unfrozen Plasma/KWin race insufficient as the root cause.  The result
does not identify a later device callback because userspace streaming stopped
after entry; it remains consistent with the earlier RTC fingerprint at RTX
device resume.  Do not repeat this configuration merely to retest the new
package policy.

The recovery boot started with `pm_test=none`, `pm_async=1`, `pm_trace=0`, no
one-shot marker, and the installed guard again blocking connected-eGPU sleep.
The GPU enumerated normally.  No full s2idle attempt was authorized.

## Primary references

- [NVIDIA 615.71.09 notifier and depth branches](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/kernel-open/nvidia/nv.c)
- [NVIDIA suspend_depth interface](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/kernel-open/nvidia/nv-procfs.c)
- [NVIDIA temporary-file allocation and writes](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/kernel-open/nvidia/os-interface.c)
- [NVIDIA video-memory preservation](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/src/nvidia/src/kernel/gpu/mem_mgr/arch/maxwell/fbsr_gm107.c)
- [NVIDIA pre-reservation of used framebuffer storage](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/src/nvidia/arch/nvalloc/unix/src/dynamic-power.c)
- [Linux tmpfs capacity, swap and huge-page options](https://docs.kernel.org/filesystems/tmpfs.html)
- Installed NVIDIA README: `/usr/share/doc/nvidia-driver/html/powermanagement.html`
- [Similar allocation-error report, not proof of the same cause](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/1309)
- [Linux PM debugging](https://docs.kernel.org/power/basic-pm-debugging.html)
- [SysRq commands and header-only console output](https://docs.kernel.org/admin-guide/sysrq.html)
- [RTC tracing also disables asynchronous device PM](https://docs.kernel.org/power/s2ram.html)
- [Upstream device-PM asynchronous scheduling gate](https://github.com/torvalds/linux/blob/master/drivers/base/power/main.c)
- [x86 ORC refuses to unwind tasks executing on another CPU](https://github.com/torvalds/linux/blob/master/arch/x86/kernel/unwind_orc.c)
- [OSTree staged deployments](https://ostreedev.github.io/ostree/deployment/)
