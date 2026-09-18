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

The last attempt was at 16:41 on Sep 18. The retained service journal shows the
one-shot guard being consumed, DisplayLink's pre-hook returning successfully,
and systemd-sleep logging `Performing sleep operation 'suspend'...`. The user
reported a cursor on the internal display, slight RTX fan activity and a hard
hang requiring power-cycle. No completion record was retained in that journal.
The accessible kernel journal does not contain enough of the transition to
identify the stalled stage. The protected per-run files still need inspection.

Selecting `pm_test=platform` does **not** prove that execution reached a
platform callback. Preparation, NVIDIA notifiers, freezer, ordinary device
suspend and late/noirq handling precede its test point. A cursor and fan speed
cannot distinguish these paths. Neither impossibility of eGPU suspend nor a
specific NVIDIA/USB4/AMD culprit has been established.

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

First improve evidence retention: inspect the existing root-only run files;
capture early notifier stacks when userspace can still run. For late/noirq
hangs, evaluate available persistent kernel logging or a suitable external
console. Larger journal quotas alone do not flush events while journald is
frozen. A kernel rebuild is not part of this plan.

1. **GPU clients and display path.** First isolate Cardwire, then compare a
   clean run without Plasma/Steam/DisplayLink clients but with NVIDIA loaded
   and the physical enclosure unchanged. If this succeeds, focus on client
   interaction, modeset/VT handoff and VRAM occupancy. If it still fails, an
   active desktop is not required. This ends the graphical session; it is a
   separate diagnostic, not a proposed permanent workflow.
2. **NVIDIA PM depth.** The 615 driver implements `modeset`, `uvm`, `default`.
   Its PM notifier uses the selected depth. Controlled **freezer-only** probes
   could distinguish display preparation, UVM/channel quiescence and full GPU
   preservation. Partial depths must never be paired with real device/platform
   sleep. The current runner deliberately accepts only `default`; extending
   it requires a separate change with matching guards.
3. **NVIDIA integration/backing-store path.** If stacks implicate preservation,
   compare notifier integration with the documented procfs/systemd integration,
   or one backing-store change at a time. Turning notifiers off without adding
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

## Primary references

- [NVIDIA 615.71.09 notifier and depth branches](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/kernel-open/nvidia/nv.c)
- [NVIDIA suspend_depth interface](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/kernel-open/nvidia/nv-procfs.c)
- Installed NVIDIA README: `/usr/share/doc/nvidia-driver/html/powermanagement.html`
- [Similar allocation-error report, not proof of the same cause](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/1309)
- [Linux PM debugging](https://docs.kernel.org/power/basic-pm-debugging.html)
- [OSTree staged deployments](https://ostreedev.github.io/ostree/deployment/)
