# Cardwire 0.12.1 safe-detach interaction

## Symptom

After the Fedora 44/Bazzite update introduced `cardwire-0.12.1`, the validated
safe-detach path ended the Plasma session but failed before unloading NVIDIA:

```text
Refusing to terminate NVIDIA holder PID 1815: UID 0, expected 1000.
```

The fail-closed ownership check was correct. Journal correlation identified
PID 1815 as the system `cardwired.service` daemon.

## Why Cardwire block is not PCI detach

Cardwire uses eBPF LSM hooks to deny new opens and metadata lookups for selected
GPU device nodes. Its own documentation explicitly describes this as operating
without unloading drivers. That makes Integrated/Hybrid policy changes
seamless for applications, but does not close an already-running compositor's
DRM file descriptors, unload NVIDIA, or remove an eGPU PCI branch.

References:

- [Cardwire eBPF design](https://github.com/OpenGamingCollective/cardwire/blob/v0.12.1/docs/development/bpf.md)
- [Linux DRM device hot-unplug requirements](https://github.com/torvalds/linux/blob/v7.2/Documentation/gpu/drm-uapi.rst#device-hot-unplug)

The root daemon also inspects GPU device nodes and can retain a descriptor after
the user graphical session has ended. Treating an arbitrary root holder as a
user process would be unsafe, so the generic holder check remains unchanged.

## Compatibility policy

The eGPU transition scripts now:

1. detect an active `cardwired.service`;
2. validate its exact systemd MainPID, process name, and `/usr/bin/cardwired`
   executable;
3. stop only that service before detach or reattach;
4. retain the fail-closed rule for every other root holder;
5. restore Cardwire's previous active state on success or rollback.

The early eGPU boot service is ordered before `cardwired.service` to prevent
the daemon from racing the guarded NVIDIA initialization. Systems without
Cardwire retain the previous transition path.

The failed-detach rollback also recreates NVIDIA character devices and restarts
`nvidia-persistenced.service` when persistence had already been stopped but the
driver itself had not been unloaded.
