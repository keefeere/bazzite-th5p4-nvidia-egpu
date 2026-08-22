# Nested Thunderbolt hotplug causes NVIDIA Xid 79 — 2026-08-21

## Scope

- Boot ID: `1f7a89e3-cf02-4f5a-8291-dec9c351e714`
- Primary enclosure: Intel TH5P4 with Zotac RTX 5070 Ti on downstream PCIe port `02:00.0 -> 03:00.0`
- Triggering device: HP Thunderbolt Dock G4, hot-added to another TH5P4 downstream port
- NVIDIA was initialized successfully at fixed Gen3 x4 and had operated normally for about 55 minutes

## Confirmed incident timeline

| Monotonic time | Event |
| ---: | --- |
| `t=3342.868s` | Existing USB hub functions on the affected downstream path disconnect. |
| `t=3351.616s` | Thunderbolt manager discovers `HP Thunderbolt Dock G4` as `0-302`. |
| `t=3352.245s` | Thunderbolt reports `failed to release unused bandwidth`. |
| `t=3352.257s` | TH5P4 PCIe downstream slot `02:01.0` reports Card present / Link Up. |
| `t=3352.380s` | Kernel starts enumerating the HP dock's Intel PCIe switch at `04:00.0`. |
| `t=3352.383s` | Kernel reports `No bus number available for hot-added bridge`. |
| `t=3352.384–388s` | PCI core cannot fit the new bridge windows and releases/reassigns parent bridge resources, including windows belonging to the live RTX path on `02:00.0`. |
| `t=3352.389s` | NVIDIA immediately reports `Xid 79, GPU has fallen off the bus`. |
| `t=3352.389s+` | NVIDIA DRM fences fail and RM/GSP enters repeated full-chip-reset/RPC failure paths. |
| `t=3419.487s` | HP dock disconnects. |
| `t=3422.805s` | HP dock reconnects, but USB3 tunnel creation fails. |

## Live state after the incident

- The host did not reboot; it remained the same boot.
- `03:00.0` still exists in PCI sysfs and the physical link still reports Gen3 x4.
- NVIDIA device nodes and `/proc/driver/nvidia/gpus/0000:03:00.0` remain present.
- `nvidia-smi` reports `No devices were found` because the loaded driver/GSP can no longer communicate with the GPU.
- KWin was restarted by its wrapper and the internal AMD-driven display remained usable.
- NVIDIA external outputs are no longer reliable; HDMI transitioned to disconnected.
- `/sys/fs/pstore` contains no crash record.

## Conclusion

This incident is not a thermal failure, ordinary USB reset, or a spontaneous
NVIDIA crash. Hot-authorizing a nested Thunderbolt dock introduced another PCIe
switch below the already-live TH5P4 fabric. The kernel lacked reserved bus
numbers and bridge resources, attempted live PCI resource reallocation, and
invalidated the active eGPU mapping. NVIDIA Xid 79 was the immediate result.

Until PCI hotplug bus/MMIO reservation is tested at boot, do not hot-add a
Thunderbolt/PCIe dock to another TH5P4 downstream port while the RTX is active.
Ordinary USB-only devices connected through a controller that does not create a
new PCIe tunnel are a separate case and were not tested by this incident.

Relevant Linux PCI command-line controls for a future isolated test include
`pci=hpbussize=`, `pci=hpmemsize=` / `pci=hpmmiosize=` /
`pci=hpmmioprefsize=`, and `pci=realloc`; exact values must be derived from the
observed topology rather than enabled blindly.

## Cold-attached HP dock comparison

A subsequent reboot with both TH5P4/RTX and HP Dock G4 connected before host
power-on succeeded (boot ID `b8f5a290-4587-4c2f-b573-df00b1815826`). The full
topology was available during initial PCI resource assignment:

```text
00:01.1 [01-60]
└─ 01:00.0 [02-0c]        TH5P4 upstream
   ├─ 02:00.0 [03]        RTX 5070 Ti + HDA
   └─ 02:01.0 [04-0a]     HP Dock G4
      └─ 04:00.0 [05-0a]  Intel Goshen Ridge TB4 switch
         ├─ 05:00.0 [06]
         ├─ 05:01.0 [07]
         ├─ 05:02.0 [08]
         ├─ 05:03.0 [09]
         └─ 05:04.0 [0a]
            └─ 0a:00.0    Intel I225-LMvP PCIe Ethernet
```

Thus the HP dock legitimately creates a PCIe tunnel even though it contains no
GPU. Its Intel Ethernet controller is PCIe, and the Goshen Ridge fabric itself
requires multiple subordinate bus numbers. With the dock present at cold boot,
the kernel reserves buses `04-0a` before NVIDIA loads and no Xid occurs. The
failed hot-add started from a smaller live topology, could not allocate those
buses/windows, and attempted disruptive bridge-resource reassignment.
