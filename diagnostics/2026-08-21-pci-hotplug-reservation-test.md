# Experimental PCI hot-plug reservation test — 2026-08-21

## Final status: rejected

Both variants failed the safety criteria:

- Firmware-numbered PCI buses left each empty TH5P4 port with only one bus.
- Adding `assign-busses` expanded each empty port only to six buses with 8 MiB
  MMIO and 8 MiB MMIO_PREF, still below the verifier's 22/32/32 requirement.
- More importantly, global bus reassignment moved the AMD iGPU from
  `64:00.0` to `19:00.0`.  `amdgpu` then reported `Unable to locate a BIOS
  ROM`, failed probe with `-22`, and the internal panel fell back to
  `simple-framebuffer` at `1366x768@60` without EDID.

`pci=assign-busses` is therefore unsafe on this machine.  The combined stable
installer no longer stages any PCI reservation policy.  HP Dock G4 must be
cold-attached behind TH5P4 while the NVIDIA eGPU is in use.

## Goal

Prevent a later HP Thunderbolt Dock G4 PCIe switch from forcing live bridge
resource reassignment while the RTX 5070 Ti is active behind the same TH5P4.

## Kernel policy under test

```text
pci=assign-busses,realloc=on,hpbussize=6,hpmmiosize=8M,hpmmioprefsize=8M
```

- `realloc=on` permits bridge windows that firmware sized too narrowly to be
  corrected during boot, before the NVIDIA driver is loaded.
- `assign-busses` is required because the first experiment proved that the
  firmware-assigned empty TH5P4 ports remain one-bus windows even with the
  other reservation parameters active.  Linux must assign and distribute the
  root port's available bus range itself.
- `hpbussize=6` is intentionally modest because it applies recursively to the
  three nested hot-plug bridges in the HP dock.
- 8 MiB non-prefetchable and prefetchable MMIO reserves are also global.  The
  post-boot verifier therefore checks the *actual* outer-port windows rather
  than assuming the requested minimum is enough.
- The staged eGPU implementation resolves the RTX, HDA function, AMD iGPU and
  enclosing TH5P4 bridges dynamically by exact PCI vendor/device IDs.  A BDF
  change caused by `assign-busses` is therefore expected and supported.

The original firmware-numbered experiment
`pci=realloc=on,hpbussize=6,hpmmiosize=8M,hpmmioprefsize=8M` produced only one
bus per empty port (`04-04`, `05-05`, `06-06`), although each port received
8 MiB MMIO and 8 MiB MMIO_PREF.  The verifier correctly rejected hot-plugging
the HP dock.

## Controlled test sequence

1. Run `install-pci-hotplug-reserve.sh` with `sudo`.
2. Power off and disconnect HP Dock G4 from the TH5P4 downstream port.
3. Cold boot with TH5P4 + RTX connected.
4. Run `verify-pci-hotplug-reserve.sh` as the regular user.
5. Do **not** hot-plug the dock unless the verifier passes.
6. For the first hot-plug test, close unsaved work and keep the internal AMD
   display available.

Rollback is `sudo ./remove-pci-hotplug-reserve.sh`, followed by a reboot.
If the experimental deployment fails before login, choose the previous
deployment from the boot menu; rpm-ostree retains it separately.
