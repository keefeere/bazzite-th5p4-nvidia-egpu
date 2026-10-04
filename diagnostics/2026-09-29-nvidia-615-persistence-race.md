# NVIDIA 615 persistence startup race — 2026-09-29

After updating Bazzite to `44.20260928.1`, kernel
`7.2.7-ogc1.1.fc44.x86_64` and NVIDIA `615.71.09`, the controlled early eGPU
loader stopped at step 4/8. PCI topology validation, the 16 GiB BAR1 repair,
the HP Dock tree and the Gen4 x4 link had all completed successfully.

`nvidia-persistenced.service` had already made five rapid attempts while the
new driver exposed `/dev/nvidia*` but still rejected an NVML device query. Its
start limit was therefore exhausted when the controlled loader called
`systemctl start`. The loader exited before loading `nvidia_drm`, leaving
`nvidia`, `nvidia_uvm` and later `nvidia_modeset` resident. The tray correctly
saw the PCI device but previously described that terminal partial stack as
initializing forever.

Live recovery confirmed the boundary: after `nvidia-smi -L` began working,
resetting and starting only `nvidia-persistenced`, loading `nvidia_drm`, and
rerunning the boot service completed the NVIDIA-first configuration without a
reboot.

The loader now polls the actual `nvidia-smi -L` handshake for up to ten
seconds. It then clears the persistence unit's stale failed/rate-limit state
and starts it normally. This is version-neutral: older drivers pass the poll
immediately. The tray separately identifies a failed boot service with core
NVIDIA loaded but DRM absent as an error rather than a transient state.
