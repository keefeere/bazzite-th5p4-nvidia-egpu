This report describes a reproducible-on-this-host symptom, not a proven NVIDIA
bug. Full diagnostic archives are not attached because they have not been
reviewed for private data; selected relevant journal lines are included below.

## System

- ASUS ROG Xbox Ally X, AMD Radeon 890M host GPU; TH5P4 USB4/Thunderbolt 5
  enclosure with GB203 GeForce RTX 5070 Ti (`10de:2c05`, PCI `0000:03:00.0`).
- Bazzite 44, Linux `7.2.4-ogc3.1.fc44.x86_64`, NVIDIA open kernel modules
  and userspace `615.71.09`; NVIDIA-first KWin Wayland session. eGPU is PCIe
  Gen4 x4 when normally booted. Optional HP Thunderbolt Dock G4 downstream.
- Ordinary boots use `UseKernelSuspendNotifiers=1`,
  `PreserveVideoMemoryAllocations=1`, `TemporaryFilePath=/var/tmp`;
  no separate NVIDIA suspend/resume systemd units. One experimental boot used
  `UseKernelSuspendNotifiers=0` with paired procfs pre/post hooks.
- Host sleep mode: `s2idle` (`mem`); reported issue also occurs with
  `pm_test=platform`, which stops short of the actual s2idle wait loop.

## Evidence

1. On Sep 19, a bounded `pm_test=devices` test with the graphical session
   intact succeeded: NVIDIA and Thunderbolt PCI suspend/resume callbacks
   returned zero, and the same KWin process continued afterward. Kernel
   device suspend took 390 ms and device resume 1489 ms. Local run ID:
   `20260919-004648-bj9awryr`.
2. Deeper `pm_test=platform` attempts repeatedly hung and needed forced host
   reset. They tried the ordinary host path, Cardwire stopped, HP physically
   absent, private 6 GiB tmpfs backing, and serialized device callbacks;
   none alone fixed it. The Sep 19 20:36 run
   `20260919-203644-cesu8z8y` used the normal
   `host_reset=Y` boot, private tmpfs, `pm_async=0`, and `pm_trace=1` for a
   one-shot RTC fingerprint. The guard consumed the exact exception and
   recorded 1810 MiB used VRAM, 13.7 GiB MemAvailable and the private mount.
   The kernel journal ends after `PM: suspend entry (s2idle)`, filesystem
   sync and two AMD DMUB hotplug IRQ messages; no completion is retained.
3. The *first* boot after that forced reset decoded
   `PM: Magic number: 0:142:402` and
   `hash matches drivers/base/power/main.c:1107`. In Linux 7.2.4,
   line 1107 is `TRACE_RESUME(0)` immediately after `TRACE_DEVICE(dev)` on
   entry to `device_resume()`. The kernel's `DEVSEED=7919`, `DEVHASH=1009`
   sdbm algorithm yields `402` for `0000:03:00.0`, the RTX function. None
   of the other PCI BDFs visible in the test boot's kernel log hash to 402.
   `device_resume()` has a second trace at line 1186 after its callback;
   it is not the retained RTC point. This suggests the last persisted PM
   trace was RTX device-resume entry, **not** that the NVIDIA callback is
   conclusively at fault: PCI parent waits, lower transport stalls, hash
   collisions or lost later RTC writes remain possible.
4. Before the test, a normal boot with `pm_trace=0` printed a *different*
   magic number (`10:762:87`) and `AMDI0052:00` matches. The run journal
   proves `pm_trace=1` at the actual guard, so the first post-test value is
   attributable to this experiment rather than just an ordinary boot hash.
5. The Sep 19 23:28 paired-procfs A/B boot loaded the RTX at Gen4 x4 and
   passed the eGPU verifier. The procfs `suspend` write succeeded before
   `pm_test=platform`; the kernel logged suspend entry. The suspend unit did
   not return, so the paired post-resume hook was never reached. A stack sample
   about 8.6 seconds after kernel entry found `systemd-sleep` in
   `pm_prepare_console -> vt_waitactive`; an earlier notifier-mode test also
   sampled this wait and later progressed, so this is **not** a proven terminal
   location. No RTC or kernel trace was enabled for the A/B. A `nvidia-drm`
   sync-FD semaphore error appeared at the lab's two-minute timeout. The user
   reported a dark internal panel rather than the earlier visible cursor.
   Forced reboot restored normal notifier mode and a working RTX. Local run
   ID: `20260919-232840-5rptq25q`.

Source audit of 615.71.09 confirms the intended A/B distinction: after a
successful procfs suspend sets `NV_FLAG_SUSPENDED`, a later PCI suspend callback
would increment `suspend_count`, and its PCI resume callback would decrement
that count without calling the full `nv_power_management(..., RESUME)` path.
The post-sleep procfs resume would perform that restore. We did not capture the
PCI callbacks in this run, so this is a source-level sequence, **not** proof
that the failing attempt reached those callbacks.

Selected journal lines from the paired-procfs test (monotonic seconds; host
name and PIDs omitted):

```text
718.505 NVIDIA procfs suspend completed; paired resume is required.
720.158 /usr/lib/systemd/system-sleep/displaylink succeeded.
720.160 PM: suspend entry (s2idle)
720.264 Filesystems sync: 0.102 seconds
833.981 Transition outcome uncertain: test settings retained until reboot.
834.164 [nvidia-drm] Failed to register auto-value-update on pre-wait value for sync FD semaphore surface
834.193 ERROR: Cycle did not finish within the deadline. No second request issued.
```

An independently reported [615.71.09 DRM flip-timeout regression on compositor
exit and VT switch](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/1361)
resembles some of this host's ordinary logout warnings. The reporter also had
successful suspend entries, so that issue alone cannot explain this host's
hard hang. Please distinguish delayed display teardown from the failed PM
cycle when triaging these logs.

The first post-reset boot also reported an ACPI thermal critical event and
hardware-protection shutdown several minutes after login, causing a second
unrequested reboot. No temperature value at the event is retained. Later
readings were around 61–63 C (zone critical trip 102 C); this may be separate
firmware/sensor behavior, not established as NVIDIA-caused.

## Questions for NVIDIA

- Is the open-module `nv_pmops_resume()` / `nvidia_resume()` path known to wait
  indefinitely for RM/GSP or PCIe completion on an external GB203 after
  late/noirq PM phases? Which existing diagnostics can distinguish a PCI
  parent wait from RM/GSP and modeset work without relying on journald after
  userspace freezing?
- Is `NVreg_UseKernelSuspendNotifiers=0` plus the documented paired
  `/proc/driver/nvidia/suspend` systemd integration supported for this open
  module and GPU? Source inspection shows the intended `suspend_count` path
  would move the full GPU restore after the kernel's PCI device-resume pass.
  The controlled paired-procfs platform test above still failed; its last
  reached kernel PM callback was not captured.
- Is there a known fix or targeted driver/GSP version for this GB203+USB4
  resume interaction? Earlier 595-series use on this host is not a controlled
  suspend comparison: the kernel and setup differed, and the retained sleep
  evidence here belongs to 615.71.09.

Recovery state must not be confused with the original suspend hang. Some
post-reset boots enumerated RTX late with only a 256 MiB BAR1, so the host's
guarded loader intentionally left NVIDIA blocked. The next post-A/B boot
instead had a working RTX, ordinary `UseKernelSuspendNotifiers=1`,
`pm_test=none`, and `pm_async=1`. No live PCI repair was attempted as part of
this report. The local run IDs identify evidence on the reporter's machine;
they are not public attachments.

## Primary source pointers

- [NVIDIA 615.71.09 PM callback and `suspend_count` paths](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/kernel-open/nvidia/nv.c)
- [NVIDIA 615.71.09 `/proc` PM interface](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/kernel-open/nvidia/nv-procfs.c)
- [Linux 7.2.4 `device_resume()` trace points](https://github.com/gregkh/linux/blob/v7.2.4/drivers/base/power/main.c)
- [Linux 7.2.4 RTC trace hash](https://github.com/gregkh/linux/blob/v7.2.4/drivers/base/power/trace.c)
