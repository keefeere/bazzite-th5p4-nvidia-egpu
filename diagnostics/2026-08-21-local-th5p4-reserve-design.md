# Local TH5P4 PCI resource reservation design — 2026-08-21

## Scope

Reserve PCI bus numbers and bridge windows only below the existing AMD USB4
root port and TH5P4.  Keep the firmware-provided topology for the AMD iGPU,
NPU, storage, Wi-Fi and every other root port.

This replaces the rejected global `pci=assign-busses` experiment.  It does not
change the rule that HP Dock G4 must not be hot-added until the resulting live
kernel resource tree passes verification.

## Clean firmware topology

```text
00:01.1 [01-60] AMD USB4 root port
└─ 01:00.0 [02-06] TH5P4 upstream
   ├─ 02:00.0 [03] RTX 5070 Ti
   ├─ 02:01.0 [04] empty; known HP Dock physical port
   ├─ 02:02.0 [05] empty
   └─ 02:03.0 [06] empty
```

The AMD root port owns buses `01-60`, but buses `07-60` are unused.  Its
bridge windows are also substantially larger than the current TH5P4 windows:

| Resource | AMD root port | Current TH5P4 / RTX | Free tail |
| --- | --- | --- | --- |
| Bus numbers | `01-60` | `02-06` | 90 additional after `06` |
| I/O | `b000-efff` | `b000-bfff` | 12 KiB |
| MMIO | `c4000000-dbffffff` | `c4000000-c80fffff` | 319 MiB |
| MMIO_PREF | `6000000000-7fffffffff` | `6000000000-6401ffffff` | about 112 GiB |

## Proposed local map

Keep the RTX branch at bus `03`, expand TH5P4 upstream to subordinate `60`,
and split the 93 buses from `04` through `60` equally:

```text
02:01.0 -> 04-22  (31 buses; HP Dock port)
02:02.0 -> 23-41  (31 buses)
02:03.0 -> 42-60  (31 buses)
```

Give the known HP Dock port the complete unused 12 KiB I/O tail plus a
conservative, aligned 128 MiB MMIO window and 1 GiB MMIO_PREF window.  Both
comfortably exceed the measured minimum while leaving most of the root port's
address space untouched.  The other two ports retain bus-number headroom but
no PCI address window in this machine-specific first version.

## Why remove/rescan is required

Writing the bridge's Secondary/Subordinate Bus Number registers alone is not
enough: the kernel also maintains a `busn_res` resource tree.  On discovery of
an already-configured bridge, Linux reads the bridge registers and inserts that
range into `busn_res`.  Therefore the safe prototype must run before NVIDIA
binds, program only the TH5P4 bridge registers, remove the TH5P4 upstream
device from the kernel's PCI device list, and rescan while the hardware remains
powered.  The sysfs `remove` operation itself does not power off the device.

Every stage must fail closed: if IDs, empty ports, free buses, parent windows,
or the post-rescan resource tree differ from the validated layout, NVIDIA must
remain unloaded and KWin must start on the AMD iGPU.

## Prototype safety boundary

`egpu-local-reserve-apply.sh` has a read-only `--dry-run` mode and refuses real
changes unless it was launched by the early `egpu-nvidia-boot.service`.  It
also refuses to run after the display manager has started or while any NVIDIA
module is loaded.  This prevents the ordinary late hot-attach path from doing
PCI bridge surgery inside a live Plasma session.

Before changing config space it requires the exact XAX layout recorded above,
not merely a vaguely compatible amount of space.  After remove/rescan,
`egpu-local-reserve-verify.sh` checks every bus range and address window before
the boot service is allowed to load NVIDIA.  A failure leaves NVIDIA blocked;
a cold reboot restores firmware bridge registers.

The experiment is enabled by `install-egpu-local-reserve-test.sh` and disabled
by `remove-egpu-local-reserve-test.sh`.  Neither script adds kernel arguments,
so the failed global `pci=assign-busses` experiment cannot return through this
path.

## First cold-boot result

The remove/rescan succeeded and Linux imported the complete bus map exactly as
planned.  Linux then normalized the bridge address windows while sizing the
RTX/VF BARs and optional hot-plug windows:

```text
01:00.0  buses 02-60  MMIO c4000000-d2ffffff  PREF 6000000000-66ffffffff
02:00.0  bus   03     MMIO c4000000-c9ffffff  PREF 6000000000-65ffffffff
02:01.0  buses 04-22  MMIO ca000000-d1ffffff  PREF 6600000000-663fffffff
02:02.0  buses 23-41  MMIO d2000000-d21fffff  PREF 6640000000-66401fffff
02:03.0  buses 42-60  MMIO d2200000-d23fffff  PREF 6640200000-66403fffff
```

All ranges are ordered, disjoint and contained by the unchanged AMD root
windows.  The original byte-exact verifier rejected the safe normalization
and correctly kept NVIDIA unloaded.  It was replaced with an invariant-based
verifier that requires the exact bus map, unchanged AMD root/iGPU, at least
12 KiB I/O + 128 MiB MMIO + 1 GiB MMIO_PREF on the HP port, containment,
non-overlap, and no endpoint already occupying buses 04-60.

## Validated hot-add result

With the local reserve active, HP Dock G4 hot-add used exactly buses `04-22`.
Its Goshen Ridge bridges and HP I225-LMvP endpoint enumerated without changing
the RTX BDF, bridge windows or link.  NVIDIA remained stable at Gen4 x4 during
the later performance experiment, and the I225 BARs were assigned inside the
reserved non-prefetchable MMIO window.  No Xid, AER fatal error or PCI bus
renumbering occurred.

USB3 tunnelling is a separate remaining issue: the Thunderbolt driver reports
`USB3 tunnel creation failed`, so HP's USB companion path currently falls back
to USB2.  This does not affect the PCIe I225 endpoint.

## Cold-attached HP Dock finding

Accepting firmware's compact cold-boot tree is not safe or deterministic.  A
cold boot produced the expected buses (`02-0c`, HP on `04-0a`) but failed to
assign the required I225 BAR0 and BAR3.  The verifier correctly blocked NVIDIA:

```text
COLD-ATTACHED HP DOCK VERIFY FAILED: 0000:0a:00.0 NIC BAR0 is not assigned
```

## Rejected authorization-cycling experiment

The first cold-dock workaround wrote `0` to the HP router's Thunderbolt
`authorized` attribute, reserved the empty TH5P4 port, and intended to
reauthorize HP afterward.  This is rejected.  Deauthorization caused a real
downstream link-down; after a subsequent warm reboot the machine sometimes
enumerated only the USB companion path while the TH5P4 Thunderbolt/PCIe router
remained absent for many minutes.  That breaks unattended reboot and can only
be recovered reliably with a full enclosure/cable power cycle.

The source and installer no longer contain any Thunderbolt deauthorization
write.

## Cold-attached PCI-only rebuild design

Cold-boot logs show that firmware imports the complete HP PCI tree before the
Linux Thunderbolt driver, boltd or the early eGPU service runs.  Consequently,
boltd policy cannot prevent the initial compact `04-0a` allocation.  The new
path keeps the already-authorized USB4 router and PCIe tunnel alive and changes
only Linux's PCI model:

1. Run before both `bolt.service` and the display manager, with NVIDIA blocked.
2. Require the exact known subtree below TH5P4 port 1: six Intel `8086:0b26`
   Goshen Ridge bridges and one HP-subsystem I225 `8086:5502`; reject anything
   else.
3. Remove only the HP upstream PCI bridge through its sysfs `remove` file.
   Recursive PCI removal unbinds `igc`, but does not change the Thunderbolt
   router's `authorized` state or reset the USB4 link.
4. Apply the already-proven TH5P4 bridge ranges and perform the existing single
   upstream remove/rescan.
5. Verify the complete HP bridge tree, I225 BARs, RTX BARs, bridge containment
   and the unchanged AMD iGPU before NVIDIA is allowed to bind.
6. On any mismatch, fail closed with NVIDIA unloaded; a cold reboot restores
   firmware bridge configuration.

`egpu-cold-hp-pci-rebuild.sh --dry-run` validates the exact live HP tree without
changing drivers, PCI state, bridge registers or Thunderbolt authorization.

## PCI-only rebuild validation — 2026-08-22

The exact-tree dry run passed with HP hot-attached.  Three subsequent boot
cases passed with TH5P4 + RTX + HP Dock left physically connected:

1. Initial controlled reboot after installing the PCI-only rebuild.
2. Warm reboot with the complete chain continuously powered.
3. Full XAX power cycle while TH5P4 and HP Dock remained powered.

Every boot logged the exact HP subtree validation, PCI-only removal, local
reserve preflight, complete cold-attached HP verification, NVIDIA initialization
and NVIDIA-first KWin order.  No Xid, AER error, link-down or inaccessible-device
event occurred after the rebuild.

The resulting live state was consistent across the tests:

- RTX remained `0000:03:00.0`, staged at PCIe Gen3 x4.
- HP occupied only buses `04-22`; unused TH5P4 ports retained `23-41` and
  `42-60`.
- HP I225 was `0000:0a:00.0`, with assigned BARs and a 1000 Mb/s link.
- The HP USB3 tunnel was live at 10 Gb/s; the StarTech DisplayLink
  `17e9:4301` enumerated at 5 Gb/s instead of USB2 fallback.
- CUDA pinned transfers measured 3.054 GB/s host-to-device and 3.366 GB/s
  device-to-host.

This validates warm unattended reboot and host-only power cycling.  A complete
power cycle of XAX, TH5P4 and HP Dock remains a separate power-loss test.

## Complete-chain power-cycle limitation

A later test fully removed power from XAX, TH5P4/eGPU and HP Dock while the two
PD-capable docks remained interconnected.  The next Linux boot correctly fell
back to AMD because no Thunderbolt or PCIe router appeared below `00:01.1`.
Only the TH5P4 USB diagnostic function `8086:1234` and HP's USB2 companion tree
enumerated.  Replugging the host USB4 cable did not recover the router, and the
RTX fans remained continuously active even with the host cable disconnected.
Recovery required a TH5P4/eGPU power cycle; Windows could also initialize the
chain after the failed state.

This failure happens below the NVIDIA driver and below the PCI-only rebuild:
there is no Thunderbolt sysfs device or PCI endpoint for software to operate
on.  It is recorded as a rare enclosure/PD/USB4 power-sequencing hang rather
than an automatic-fix target.  Supported unattended operation is therefore:

- warm host reboot with the complete chain continuously powered; and
- XAX-only shutdown/power-on while TH5P4 and HP remain powered.

For complete-chain recovery, fully discharge TH5P4, isolate the downstream HP
Dock while bringing TH5P4/eGPU up, then reconnect the host and HP in a known
working order.  Exact cold power sequencing remains hardware/firmware-specific.

## Final Gen4 validation and policy

The complete current configuration was subsequently tested at PCIe Gen4 x4,
including the local TH5P4 reserve and a cold-attached HP Dock PCI-only rebuild.
The boot completed with NVIDIA as KWin primary, the entire HP PCI/USB3 tree
present, and no NVIDIA Xid, AER or link-down event.  Measured CUDA pinned-memory
transfers were:

```text
PCIe link:             16.0 GT/s x4
Pinned host -> device: 3.866 GB/s
Device -> pinned host: 3.910 GB/s
```

Gen4 is therefore the final preferred profile, but it is not made an
unconditional boot default.  `/etc/egpu-nvidia/try-gen4-once` is consumed
before link retraining; only a fully successful early Gen4 initialization may
re-create it when `/etc/egpu-nvidia/use-gen4` exists.  A failed or hung Gen4
transition consequently leaves the following boot on Gen3 until the
administrator explicitly re-arms Gen4.  This avoids an unattended boot loop.

The final System Tray status helper also recognizes the complete-chain hang:
if USB `8086:1234` is present while the exact TH5P4 Thunderbolt router is
absent, it reports that the enclosure requires a power cycle instead of merely
claiming that the eGPU is disconnected.
