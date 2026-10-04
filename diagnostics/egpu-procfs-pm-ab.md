# NVIDIA procfs PM ordering A/B — repository prototype

Status: **one bounded hardware A/B failed on Sep 19**. The one-shot request
was consumed, and the following boot returned to ordinary notifier mode.
The sleep guard remains active. Do not turn
`NVreg_UseKernelSuspendNotifiers` off without paired suspend/resume integration.

The prototype consists of `egpu-nvidia-procfs-pm.py`,
`95-egpu-nvidia-procfs-pm.conf`, `nvidia-base-only-procfs-pm.conf`, a
one-boot selector in the controlled loader, and the explicit staging utility
`egpu-procfs-pm-ab.sh`. The latter was run once. An unarmed loader
still selects `UseKernelSuspendNotifiers=1`. The one-boot request is consumed
before the first NVIDIA load, and a marker in `/run` pins that mode for
controlled retries in the same boot; the following boot defaults back to
notifier mode.

The drop-in's `ExecStartPre` follows the existing
`90-egpu-sleep-guard.conf`; `ExecStopPost` invokes resume even if a later sleep
start fails. The hook is a no-op when the one-boot mode is inactive. The
staging utility first requires a healthy ordinary eGPU/verifier state,
`pm_test=none`, RTC tracing off, no conflicting A/B mode, and no other NVIDIA
PM service or system-sleep procfs hook that could issue duplicate writes. It installs the
paired files and checks the *loaded* systemd order before creating the latch.
The loader then verifies that order again
before consuming it and verifies the live NVIDIA parameter after module
load. This ordering follows `systemd.service(5)`, but has not been exercised
on this host for a successful sleep/resume cycle. Do not install the drop-in
alone or manually write `suspend` to procfs.

The sleep lab and its private-VRAM checker recognize the paired mode only
when the boot marker, live parameter, installed helper, and effective guard /
pre / post ordering match; ordinary notifier mode is unchanged. Hardware-free
tests cover the one-boot latch, config delta, hook pre/post recovery, bad
markers, and diagnostic acceptance/rejection. They do **not** establish that
the GPU can survive `pm_test=platform` or real s2idle.

## Sep 19 paired-procfs result

Boot `bc0ef247-0d28-4e73-baf9-222a8d153a9a` loaded NVIDIA 615.71.09
with `UseKernelSuspendNotifiers=0`, the one-boot marker, effective guard/pre/post
hook order, a passing eGPU verifier and Gen4 x4. The read-only sleep-lab check
identified `nvidia_pm_transport=paired-procfs`. One explicitly authorized
`pm_test=platform` invocation used the same private 6 GiB tmpfs and `pm_async=0`
comparison settings as the prior notifier trial, without RTC tracing:
`/var/log/egpu-sleep-lab/20260919-232840-5rptq25q`.

The guard accepted exactly one exception; the procfs `suspend` write returned
success at monotonic 718.505. `systemd-sleep` entered the kernel at 720.160.
At 728.761, a userspace sample caught it waiting in
`pm_prepare_console -> vt_move_to_console -> vt_waitactive`. **This sample does
not locate the terminal hang**: an earlier notifier-mode run was sampled in
the same VT wait and subsequently advanced to the PM notifier chain and
freezer. No kernel PM trace or RTC fingerprint was enabled for this A/B.
The suspend unit never completed, so its paired `ExecStopPost` resume could
not run. The lab timed out at about 23:30:42 and intentionally retained
`pm_test=platform` and `pm_async=0` until reboot. A `nvidia-drm` sync-FD
semaphore error appeared at the timeout, but it does not establish the first
stalled callback. The user reported that the internal display was off, unlike
earlier cursor-on-panel hangs. A forced reboot cleared the diagnostic settings;
boot `b8e04ab3-93d6-4d45-b473-f679397c980a` has an available RTX and
`UseKernelSuspendNotifiers=1` again. The A/B did not achieve S2/s2idle recovery;
do not repeat it without new evidence or a changed variable.

## Why this is a meaningful comparison

The Sep 19 RTC fingerprint resolves to `device_resume()` entry for RTX
`0000:03:00.0`. In NVIDIA 615.71.09, the PCI `nv_pmops_resume()` calls
`nvidia_resume()`. Under today's notifier mode, that call can perform
`nv_power_management(..., RESUME)` and `nvidia_modeset_resume()` before the
kernel has finished the device-resume pass.

NVIDIA also documents `/proc/driver/nvidia/suspend`. If the GPU was suspended
through that interface before systemd enters the kernel, the later PCI suspend
callback increments `suspend_count`; the PCI resume callback decrements it
without the full GPU restore. A paired post-sleep write of `resume` then
performs the full restore after kernel device resume. This is a plausible
ordering workaround, **not** evidence that it will work on this USB4 eGPU.
The same GSP/PCIe operation could still hang later.

Post-test source recheck of the exact 615.71.09 open module confirms that
`nvidia_suspend()` increments `suspend_count` when the procfs path has already
set `NV_FLAG_SUSPENDED`, and `nvidia_resume()` then decrements a nonzero count
without calling `nv_power_management()` or `nvidia_modeset_resume()` in that
PCI callback. The later procfs `resume` is therefore the path intended to do
the full restore. In our failed A/B, the pre-hook succeeded but the suspend
unit never returned to run the post-hook. Thus the A/B changed the intended
ordering yet did not complete platform PM; the available trace still cannot
separate an earlier console/freezer/PCI stall from the eventual restore path.

## Required safety properties before implementing or trying it

1. Change only the controlled loader's NVIDIA module option for one explicitly
   armed boot. Consume the persistent request before module load, retain an
   active marker in `/run`, and restore notifier mode automatically on the
   following boot. Verify the live module parameter is exactly `0`; never
   unload a live GPU merely to switch modes.
2. Keep the ordinary eGPU sleep guard. The one-shot diagnostic exception must
   be checked and consumed **before** writing `suspend` to NVIDIA procfs.
   A separate systemd dependency that starts a vendor suspend service before
   `systemd-suspend.service`'s existing `ExecStartPre` guard is unsafe: a guard
   refusal could leave the GPU suspended.
3. Use paired pre-sleep `suspend` and post-sleep `resume` writes, with a
   recovery path after a refused or failed system sleep. Verify ordering on a
   no-PM/hardware-free test before any live run. Missing procfs, wrong driver,
   already-pending PM, wrong boot marker, or a failed write must fail closed.
4. Run only one bounded `pm_test=platform` comparison before considering real
   s2idle. Do not combine it with Gen3, host-reset, no-graphics, or other A/B
   switches. Preserve the same session, driver and PCI topology when possible.
5. Stop if TH5P4's router is absent, NVIDIA cannot initialize normally, or
   thermal behavior is abnormal. The Sep 19 RTC attempt was followed by a
   `thermal_zone1` critical shutdown; its cause remains unproven.

Before staging this A/B on the host: recover the normal 16 GiB BAR1 and a
passing `nvidia-smi`/eGPU verifier, rule out ongoing thermal trouble, and
review the installed file diff. The installation/next boot and one risky
`pm_test=platform` invocation require separate decisions. On an armed boot,
the read-only lab preflight should use the same private-tmpfs/serial settings
as the prior comparison, without RTC trace. If the platform stage returns
cleanly *and* the graphical session and GPU remain usable, only then consider
a separately authorized real s2idle trial. The goal remains a normal sleep
and resume with the desktop session intact; this A/B is only a diagnostic.

Primary code: [NVIDIA 615 PM callbacks and suspend count](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/kernel-open/nvidia/nv.c),
[NVIDIA procfs PM parser](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/kernel-open/nvidia/nv-procfs.c),
[NVIDIA PM integration guidance](https://download.nvidia.com/XFree86/Linux-x86_64/615.71.09/README/powermanagement.html).
