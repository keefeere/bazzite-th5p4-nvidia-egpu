# Guarded NVIDIA eGPU stack for Bazzite

This is a profile-driven installer distilled from a tested, machine-specific
Bazzite setup. It is shareable and reversible, but it is not a generic eGPU
autodetector. Its most unusual feature—rewriting local PCI bridge bus numbers
and windows—is safe only when the supplied hardware profile exactly describes
the host and enclosure topology.

The bundled profile is for:

```text
ASUS ROG Xbox Ally X
└─ TH5P4 / JHL9480
   ├─ Zotac RTX 5070 Ti SFF OC
   └─ HP Thunderbolt Dock G4 (optional downstream profile)
      ├─ Intel I225 Ethernet
      └─ USB3 / StarTech DisplayLink
```

The stack preserves firmware PCI numbering, reserves buses and bridge windows
only below the configured eGPU enclosure, handles a known cold-attached dock
through a kernel-version-specific guarded path without deauthorizing its USB4
router, initializes NVIDIA before KWin, and provides a safe-detach Plasma
widget.

## Safety boundary

- Do not install the bundled profile on different hardware unchanged.
- Do not guess PCI window values. Capture the clean topology and validate it.
- The scripts refuse ambiguous devices, unexpected ancestry, BDF changes and
  incomplete dock trees. A failed local transition leaves NVIDIA blocked so
  the configured iGPU remains the fallback.
- No global `pci=assign-busses` or Thunderbolt deauthorization is used.
- Keep a previous rpm-ostree deployment available in the boot menu.

## Requirements

- Bazzite or another rpm-ostree Fedora image with the proprietary NVIDIA stack
- KDE Plasma 6 and systemd
- `bash`, `pciutils`, `jq`, `polkit`, `rpm-ostree`, `systemd`, `udev`
  and the NVIDIA utilities (`nvidia-modprobe`, `nvidia-smi`)
- one local Plasma account identified by name and numeric UID in the profile

The read-only preflight reports any missing command before installation.

## Kernel compatibility

The installer selects the PCI rescan behavior from the running kernel release:

- kernels older than 7.2 keep the original validated path unchanged, without a
  project-managed PCI sizing argument;
- Linux 7.2 and newer use the same local TH5P4 register programming and strict
  post-rescan verifier, plus the exact
  `pci=hpmmiosize=32M,hpmmioprefsize=32M` policy. Linux 7.2 no longer preserves
  an oversized empty bridge window, so the measured 32 MiB dock minimum must
  be supplied through the allocator's standard hot-plug sizing inputs.

This is deliberately not a return to global PCI reassignment. A tested
`pci=realloc=on` attempt repacked TH5P4 to buses `02-06` before the early
service and was rejected. The stack never adds `pci=assign-busses` or
`hpbussize`; firmware numbering and the AMD iGPU BDF remain untouched. The
32 MiB values affect only how Linux sizes unassigned hot-plug memory windows;
the machine-specific verifier still requires the exact local bus map,
containment and non-overlap before NVIDIA can load. If the selected kernel
argument is absent or the rejected realloc argument is active, the service
fails closed.

Once the exact TH5P4 router is visible, the bundled profile allows up to 12
seconds for its RTX endpoint. A cold-attached downstream HP Dock was measured
to push enumeration to the former 5-second boundary. This bounded wait is not
used when TH5P4 is absent, so it does not slow an ordinary AMD-only boot. An
adapted profile may set `EGPU_ENDPOINT_WAIT_SECONDS` from 1 through 30.

Boot ordering is also selected by kernel generation. The validated legacy path
keeps eGPU staging before `bolt.service`. On Linux 7.2 and newer the upstream
USB4 host reset can replace the initramfs PCIe tunnel, so `boltd` is started
first to authorize the replacement tunnel; the bounded endpoint wait then
runs before Cardwire and the display manager. This avoids an ordering deadlock
without changing the older-kernel path.

The Linux 7.2 early ReBAR repair supports both validated cold-boot layouts.
With the downstream HP Dock present it preserves that exact PCI branch and
temporarily releases only the two empty TH5P4 ports. With the optional dock
absent, all three downstream ports must be empty and match the profile before
they are temporarily released. Both paths request the full 16 GiB RTX BAR1,
restore and verify the local bus map, and only then allow NVIDIA to bind. The
no-dock rebuild quarantines `pciehp` on the empty HP-facing port until reboot,
because its rebuilt I/O window is too small for safe PCIe dock hot-add; the
eGPU and enclosure USB remain fully available.

The installer records ownership only when it adds the exact
32 MiB sizing argument itself. A rerun after booting an older kernel removes
that project-owned argument and restores the legacy behavior. The installer
also migrates away the exact `pci=realloc=on` argument owned by the rejected
earlier compatibility attempt. Pre-existing user-owned arguments are never
silently deleted. After a major kernel update, rerun the installer and reboot
before judging eGPU output.

The controlled loader uses an isolated modprobe configuration so Bazzite cannot
auto-load `nvidia_drm` before PCI staging completes. Before loading the NVIDIA
core module it now inspects the active deployment's normal modprobe policy. If
that deployment requests
`NVreg_RegistryDwords=RMDisableNoncontigAlloc=1`, the loader mirrors the exact
option for that load. This preserves Bazzite's contiguous-allocation workaround
for Gamescope scanout corruption without hard-coding a new-driver policy into
older deployments. The verifier checks both the host request and the live
`RegistryDwords` value; changing this policy requires a reboot before the live
check can pass.

For a suspend-allocation A/B diagnostic, the installed
`egpu-arm-suspend-policy-test.sh` can arm one boot which omits only
`RMDisableNoncontigAlloc=1` from the controlled NVIDIA load. The latch is
consumed before module loading and represented by a `/run` marker for the rest
of that boot, so retries cannot silently change policy. The following reboot
automatically restores the deployment policy. This diagnostic can reintroduce
the Gamescope scanout corruption that the Bazzite option prevents:

```bash
sudo /etc/egpu-nvidia/egpu-arm-suspend-policy-test.sh arm
# Historical diagnostic; this did not fix suspend on the tested machine.
sudo /etc/egpu-nvidia/egpu-arm-suspend-policy-test.sh cancel  # cancel before reboot
```

## Install the bundled profile

From this directory:

```bash
sudo ./install-egpu-all.sh
```

An existing `/etc/egpu-nvidia/hardware.conf` is preserved on a normal rerun.
To deliberately install or replace it with another reviewed profile:

```bash
sudo ./install-egpu-all.sh --config /absolute/path/to/hardware.conf
```

Leave the validated chain powered and connected, perform a warm reboot, then
run:

```bash
/etc/egpu-nvidia/verify-egpu-install.sh
```

## Adapt the profile for another machine

Generate a read-only discovery report on the target machine:

```bash
./egpu-hardware-report.sh > hardware-report.txt
```

Copy `hardware.conf`, then edit the copy. It contains:

- display names and the desktop account;
- GPU, audio, iGPU, enclosure, router, dock, NIC and USB IDs;
- the exact clean BDF ancestry;
- firmware and reserved bus ranges;
- measured bridge windows and required BAR sizes.

Validate the candidate without changing the machine:

```bash
EGPU_CONFIG_FILE=/absolute/path/to/hardware.conf ./egpu-config-preflight.sh
```

A passing preflight proves that the syntax and visible topology match. It does
not prove that newly chosen bridge windows are safe. For untested hardware,
review `diagnostics/2026-08-21-local-th5p4-reserve-design.md` and first exercise
the local-reserve preflight with NVIDIA unloaded. The project intentionally
does not infer or program novel resource layouts automatically.

Set `HP_DOCK_SUPPORT=0` when there is no validated downstream PCIe dock. Dock
tree checks and the cold-attached compatibility path are then disabled while
the eGPU path remains available.

## Tray language and safe unplug

Plasma does not translate custom widget strings automatically. The widget has
explicit English and Ukrainian text: a Ukrainian Plasma locale selects
Ukrainian; every other locale falls back to English. Hardware names come from
`hardware.conf`.

Use **NVIDIA eGPU → Safely detach** (or **Безпечно від’єднати**) in the Plasma
System Tray. It ends the graphical session, restarts an iGPU-only login screen,
unbinds NVIDIA HDMI audio and unloads all NVIDIA modules. Physically unplug only
after the widget or this file says it is safe:

```bash
cat /run/egpu-safe-to-unplug
```

Ending the session is deliberate: KWin is started NVIDIA-first for smooth
high-refresh, HDR and VRR output. Removing its primary DRM device live is not a
supported promise. After unloading NVIDIA, the detach path removes only the
validated GPU bridge and its GPU/audio children from Linux's PCI model. It does
not remove the enclosure upstream, touch sibling dock devices, reset the
enclosure or change Thunderbolt authorization. This prevents desktop power
helpers from probing a live but driverless GPU. While the released enclosure
remains physically connected, the widget offers **Connect eGPU** / **Підключити
eGPU**; this performs a validated upstream rescan, clears the latch and starts
a new NVIDIA-first session at conservative Gen3. On Linux 7.2+, a child-only
rescan can incorrectly give the RTX I/O window to an unused TH5P4 hot-plug
port. In that compatibility mode, reattach first validates and recycles only
the released RTX port plus the two exact empty sibling ports. The active HP
Dock branch is not removed. Pre-7.2 kernels retain the previously validated
child-only rescan.

Physically unplugging the enclosure invalidates the runtime bridge reservation.
On this validated machine, a same-boot cable replug enumerates the RTX with a
256 MiB BAR1 instead of 16 GiB. Safe detach is therefore a supported one-way
operation: after the widget permits unplugging, cable removal is successful,
but the next NVIDIA use requires a reboot with the enclosure attached. The
tray reports this as **eGPU safely disconnected**, not as a PCI failure. If the
cable is reinserted in that boot, NVIDIA stays blocked and the tray asks for a
reboot. While the cable has never been removed, the existing **Connect eGPU**
action may still reverse a same-cable detach through its separately validated
RTX-port rescan.

A fresh physical hot-plug after an AMD-only graphical boot is never activated
automatically. On Linux 7.2+ the add event deliberately leaves the endpoint
driverless so connecting a cable cannot unexpectedly end the current session
or mutate live PCI resources. When the exact configured topology appears with
the validated driver-free 256 MiB BAR1 state, the tray offers **Connect eGPU**
as an explicit guarded action. It preserves the configured HP Dock branch,
ends the graphical session, requests a 16 GiB BAR1 through the kernel's
`resource1_resize` interface and starts a new NVIDIA-first session at
conservative Gen3. The service validates every precondition again and fails
closed to the AMD login screen. Older kernels, a changed topology, stale
reserve state and a cable replug after completed safe-detach continue to
require a reboot.

When the live repair runs without the HP Dock, the 7.2 allocator leaves
only 4 KiB of I/O on each empty downstream port, less than the complete HP Dock
PCI tree was validated with. The repair therefore unbinds `pciehp` only from
the exact HP-facing port until reboot. NVIDIA outputs and enclosure USB remain
available, but HP Dock PCIe/Ethernet hot-add is deliberately disabled for that
experimental boot. The live repair uses conservative Gen3. Older kernels and
any topology outside the strict profile require a reboot rather than
attempting ReBAR.

The profile also loads `nvidia_drm` with `fbdev=0`. The integrated GPU remains
the fallback VT/framebuffer device; otherwise NVIDIA's kernel framebuffer can
retain `nvidia_drm` after Plasma exits and make safe module unload impossible.
This changes only the text/fallback console owner—NVIDIA DRM modesetting remains
enabled for KWin and the external displays. A detach failure restores the
previous NVIDIA-first KWin order before restarting the display manager.

Bazzite images that include [Cardwire](https://github.com/OpenGamingCollective/cardwire)
are handled explicitly. Cardwire's eBPF GPU block prevents new applications
from opening a device, but it is not a PCI/driver detach and the root
`cardwired` daemon can itself retain NVIDIA device nodes. The transition
scripts validate and temporarily stop exactly `cardwired.service`, then restore
its previous active state after detach, reattach, or rollback. The early eGPU
service is also ordered before Cardwire so the daemon cannot race the guarded
NVIDIA initialization.

## Guarded Gen4

A Gen4 request is consumed before link retraining and re-created only after a
fully successful early Gen4 boot. If a Gen4 transition fails, the next boot
uses Gen3 and remains there until Gen4 is explicitly enabled again:

```bash
sudo /etc/egpu-nvidia/enable-egpu-gen4.sh
```

Select stable Gen3 for future boots without changing the live link:

```bash
sudo /etc/egpu-nvidia/disable-egpu-gen4.sh
```

## Validated power flows and known limitation

- warm host reboot with the complete chain left powered and connected;
- host-only shutdown/power-on while enclosure and dock remain powered;
- full cold boot with the complete enclosure and dock chain already attached;
- full cold boot with TH5P4 and RTX attached while the optional HP Dock is
  absent;
- safe release and physical unplug, with a reboot required before reuse;
- same-cable detach and reattach without physically removing the USB4 cable;
- guarded first physical hot-attach on Linux 7.2+ through the explicit tray
  action, with a session restart and conservative Gen3 link.

Automatic physical eGPU activation, same-boot replug after cable removal and
hot-adding the complete downstream PCIe dock remain outside the supported
production lifecycle. Boot with the required chain already connected unless
the tray explicitly offers the guarded first-hotplug action.

A rare full-chain power-cycle can leave only the configured enclosure's USB
diagnostic function visible while its USB4/PCIe router and GPU never enumerate.
Constant GPU fan operation is another symptom. This occurs below Linux PCI and
NVIDIA, so the scripts do not attempt a risky reset. Fully remove enclosure/GPU
power, allow its capacitors to discharge, bring it up without the downstream
dock, connect the host, then reconnect the dock. The tray reports the state and
recovery requirement in the selected language.

## Optional temporary sleep guard

Suspend/resume with this enclosure is not validated. To refuse sleep while the
profiled GPU or TH5P4 enclosure is detected:

```bash
sudo ./install-egpu-sleep-guard.sh
```

This takes effect immediately, without rebooting or reloading NVIDIA. An
`ExecStartPre` check prevents `systemd-sleep` from running for suspend,
hibernate, hybrid sleep and suspend-then-hibernate. Screen locking and other
preparatory services can still run; a blocked attempt reports a service failure.
Display blanking, shutdown and reboot are unaffected. This covers the standard
systemd sleep services, not direct root writes to the kernel power interface.

Logical detach alone does not unblock sleep: the enclosure's Thunderbolt router
or USB diagnostic function also counts as present. After safe physical unplug,
the next sleep attempt is allowed automatically. An HP dock by itself does not
block sleep. Unreadable/missing profile or missing sysfs buses fail closed.

To inspect the decision without requesting sleep:

```bash
bash /etc/egpu-nvidia/egpu-sleep-guard.sh
sudo journalctl -b -u systemd-suspend.service -n 30
```

To remove only this temporary guard (no reboot required):

```bash
sudo ./remove-egpu-sleep-guard.sh
```

Full stack rollback also removes these drop-ins. The guard is deliberately
opt-in and is not automatically enabled by the main installer.

Direct invocation of the guard is read-only. The diagnostic exception described
below is consumed only by `systemd-suspend.service`, never by hibernate, hybrid
sleep, suspend-then-hibernate or a manual status check. Reinstall the optional
guard to migrate older drop-ins to this unit-scoped form.

## Explicit USB4 host-reset sleep experiment (7.2+ only)

**Diagnostic history, not a working suspend fix.** The no-dock experiment
booted successfully but its `platform` test still hung on 2026-09-18. See the
[results and remaining hypotheses](diagnostics/2026-09-18-suspend-status.md).

`sudo ./egpu-host-reset-test.sh arm` stages `thunderbolt.host_reset=0` with
`egpu.host_reset_test=1`, after refusing pending deployments and existing reset
overrides. It backs up and installs only the diagnostic-aware helpers.
The cold-boot ReBAR path accepts this exception only with both exact arguments
and an actual `host_reset=N`; all topology, driver and resource checks remain.
The verifier reports a diagnostic warning rather than treating it as production.
Legacy kernel policy and the main installer's normal policy are unchanged.

Reboot with the same powered chain attached. Check `nvidia-smi -L`, the full
verifier and `./egpu-host-reset-test.sh status`. Do not test hot-plug or rerun
the main installer during this experiment. The sleep guard stays installed.

This is **not an automatic one-shot**. After boot, **before any sleep test**, run
`sudo ./egpu-host-reset-test.sh cancel` to finalize removal of the diagnostic
arguments for the following boot. This leaves the running experiment intact
but makes the next normal boot use the standard policy again. Confirm the
finalization succeeded before attempting sleep. If the experimental boot fails,
select the previous deployment in the boot menu. Do not repeat sleep tests or
disable the guard until the graphics and rollback state have been checked.

`cancel` removes no unmarked reset setting and does not replace the helper
files: their inactive diagnostic branch is harmless without the boot marker.

### No-dock, existing-resource variant

The first `host_reset=0` trial with HP attached stopped before NVIDIA loading:
the root I/O aperture was 16 KiB rather than the cold-dock path's required
32 KiB, and TH5P4 had a compact `02-0c` bus range. This did not test suspend.

For the isolated enclosure variant use `sudo ./egpu-host-reset-test.sh arm-nodock`.
Then shut down XAX, physically disconnect HP from TH5P4, and boot with only
powered TH5P4 + RTX already connected. Do not change the GPU cable or other
power policies simultaneously. Do not attach any downstream PCIe device in
this boot or rerun the full stack installer.

This adds the separate `egpu.host_reset_nodock=1` marker. Only this variant
bypasses the normal local reservation/ReBAR path. It requires the same exact
GPU/audio/bridge identities and ancestry, no downstream devices, complete
16 GiB BAR1, correctly typed/aligned BARs, nested bus/window ranges and no
sibling resource overlap. It accepts valid compact bus ranges and existing
addresses rather than requiring the production reservation map. It never
resizes BARs, programs bridge windows, removes devices or rescans PCI.
Insufficient resources leave NVIDIA blocked, with actual resources logged.

Before loading NVIDIA it unbinds pciehp only on the three validated empty
TH5P4 downstream ports, preventing PCIe hot-add until reboot. Ordinary GPU
initialization, link staging and D0 policy remain unchanged. The read-only
checker has synthetic-tree tests in `tests/test-existing-resources.py`.
Physical boot succeeded on the test host; suspend did not complete.

After boot check the full verifier and `nvidia-smi -L`, then run `cancel`
**before any sleep test** to finalize the normal next boot. The sleep guard is
not removed by this helper. If this variant cannot boot, use the previous
deployment. If it boots only on AMD, collect
`journalctl -b -u egpu-nvidia-boot.service` before cancellation/reboot.

`cancel` also invokes OSTree's `admin finalize-staged` command and
verifies that no staged deployment or marked reset argument remains. This is
required before a deliberately risky suspend test: a hard power-cycle cannot
run the normal shutdown-time `ostree-finalize-staged.service`, so merely
staging the recovery kargs would otherwise boot the experimental deployment
again after a hang.

The explicit diagnostic runner and its hardware-free tests are now versioned
in this repository, rather than requiring a private workspace copy. See
[sleep-lab instructions](diagnostics/egpu-sleep-lab.md). Nothing installs or
runs that diagnostic through the normal eGPU boot service.

## Logs and support bundle

Useful read-only commands:

```bash
/etc/egpu-nvidia/verify-egpu-install.sh
/etc/egpu-nvidia/egpu-hardware-report.sh > hardware-report.txt
sudo journalctl -b -u egpu-nvidia-boot.service
sudo journalctl -b -k
```

Include the profile with secrets removed if any locally added comments contain
them. The parser accepts only declarative `KEY=value` records (quoted text or
integer values); shell expansion and executable commands are rejected.

## Rollback

```bash
sudo ./remove-egpu-all.sh
```

Rollback stages a new rpm-ostree deployment and requires a reboot. It removes
only this stack's exact NVIDIA initramfs omission while preserving unrelated
dracut arguments such as encrypted-root support. The retired global PCI
reservation experiments (`install-pci-hotplug-reserve.sh` and
`verify-pci-hotplug-reserve.sh`) are retained only as diagnostic history and
must not be combined with the local profile. It removes `pci=realloc=on` only
when the version-aware installer recorded that this project added it, and uses
the same ownership rule for the Linux 7.2+ 32 MiB sizing argument.

## Acknowledgements

[all-ways-egpu](https://github.com/ewagner12/all-ways-egpu) by
[@ewagner12](https://github.com/ewagner12) was an early reference and source of
inspiration for making an eGPU primary on Wayland, particularly the compositor
primary-GPU environment approach and the `thunderbolt.host_reset=0` workaround.
That workaround remains enabled only for the original pre-7.2 validated kernel
path. Linux 7.2+ uses the upstream host-router reset default after a controlled
A/B test showed that it is required for reliable first TH5P4 hot-plug on the
profiled AMD USB4 host.
No `all-ways-egpu` source code is vendored here; this project's hardware-specific
PCI resource staging, NVIDIA initialization, downstream-dock handling and safe
detach flow are separate implementations.

License: GPL-2.0-or-later.
