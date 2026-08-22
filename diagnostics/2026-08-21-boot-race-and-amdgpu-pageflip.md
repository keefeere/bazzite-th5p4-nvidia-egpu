# eGPU boot race and AMD pageflip freeze — 2026-08-21

## Systems under test

- Host: ASUS ROG Xbox Ally X / AMD Strix iGPU
- eGPU: Zotac GeForce RTX 5070 Ti SFF OC
- Enclosure path: Intel JHL9480 / TH5P4 over USB4
- OS: Bazzite Deck NVIDIA 43.20260420
- Kernel: `6.17.7-ba29.fc43.x86_64`
- NVIDIA driver: `595.58.03` open kernel module
- Kernel arguments relevant to USB4: `thunderbolt.host_reset=0 thunderbolt.clx=0`
- Stable workaround target: fixed PCIe Gen3 x4 before the NVIDIA modules load

## Boot identifiers

- Failed/frozen boot: `dec99addb5a34b1f934688cb40de417a`
- Current successful boot: `45b352305f854a8ba78c9f10bcfb360a`
- Late-endpoint quarantine boot: `2577d512-069e-43b2-b0fe-6bbc9f2b56ad`
- Fast-policy successful boot: `1f7a89e3-cf02-4f5a-8291-dec9c351e714`

Wall-clock timestamps in the failed boot moved by approximately three hours while the system initialized. All comparisons below therefore use journal monotonic timestamps.

## Failed boot: confirmed timeline

| Monotonic time | Event |
| ---: | --- |
| `t=4.831s` | USB4 connection manager discovers `Intel TH5P4` and begins retimer discovery. |
| `t=11.945s` | `egpu-nvidia-boot.service` starts waiting for `0000:03:00.0`. |
| `t=20.043s` | The current 8-second wait expires. The service reports the RTX absent and permits an AMD-only graphical login. |
| `t=39.095s` | USB4 PCIe root slot finally reports `Card present` and `Link Up`. |
| `t=39.191s` | `nvidia-settings` reports that the NVIDIA driver is not loaded. |
| `t=39.228s` | RTX `10de:2c05` and NVIDIA HDA `10de:22e9` enumerate at `03:00.0/1`. |
| `t=39.251s` | `snd_hda_intel` binds automatically to the NVIDIA HDA function. |
| `t=158–164s` | ChatGPT/Electron starts. Its renderer reports two failures to obtain VSync parameters. |
| `t=164.922s` | KWin emits the first `Pageflip timed out! This is a bug in the amdgpu kernel driver`. |
| `t=164.922–268.954s` | The same pageflip timeout repeats almost exactly once per second; the graphical session is effectively frozen. |
| `t=266.257s` | A short hardware power-key press is recorded. |
| `t=266.591s` | `systemd-logind` starts suspend in response to that key press. |
| `t=268.443s` | Kernel enters `s2idle`; the journal never records a resume. |

## What did not happen in the failed boot

- The NVIDIA kernel driver did not load. There are no `NVRM`, `nvidia-modeset`, or `nvidia-drm` initialization records.
- The Gen3 staging script did not run because the endpoint arrived after the boot service had already exited.
- There is no NVIDIA Xid, AER, PCIe Bus Error, `RmInitAdapter`, GPU-fallen-off-bus, MCE hardware error, watchdog lockup, or RCU-stall record.
- `/sys/fs/pstore` contains no crash record.
- Suspend was initiated after the pageflip failure had already repeated for about 101 seconds. Suspend was a recovery attempt, not the original cause of the graphical freeze.

## Current successful boot: comparison

| Monotonic time | Event |
| ---: | --- |
| `t=1.983s` | RTX PCIe endpoint is already enumerated at `03:00.0`. |
| `t=16.376s` | Boot staging service starts. |
| `t=16.471s` | Link is observed at Gen4 x4 before staging. |
| `t=16.542s` | Bridge and GPU are verified at fixed Gen3 x4. |
| `t=16.892s` | NVIDIA core module is initialized. |
| `t=18.651s` | NVIDIA DRM/KMS is initialized. |
| `t=18.752s` | `nvidia-smi -L` succeeds; late HDA bind and KWin NVIDIA-first configuration complete. |

Live verification on the successful boot:

- Bridge `02:00.0`: `8.0 GT/s`, width `x4`
- RTX `03:00.0`: `8.0 GT/s`, width `x4`
- OpenGL vendor/renderer: NVIDIA / GeForce RTX 5070 Ti
- KWin and desktop applications are visible in `nvidia-smi`
- No Xid, AER, or PCIe fatal signatures

## Evidence-backed conclusions

1. There is a real USB4 boot-enumeration race. The RTX can be present before the kernel starts, or the PCIe tunnel can appear roughly 39 seconds after boot even though the TH5P4 router was visible at about 5 seconds.
2. The current 8-second endpoint wait is insufficient. It allowed SDDM/KWin to start on AMD while the eGPU PCIe tunnel was still being constructed.
3. When this happens, the eGPU arrives in an unmanaged partial state: NVIDIA remains deliberately blocked, Gen3/D0 staging is skipped, but NVIDIA HDA auto-binds.
4. The observed freeze in this boot is an AMD/KWin pageflip stall, not an NVIDIA Xid/reset. Late eGPU enumeration is strongly correlated with the unsupported partial state, but the journal alone does not prove which PCIe/HDA/power event caused the later amdgpu pageflip failure.
5. ChatGPT/Electron rendering immediately preceded the first timeout and may have exposed the stuck output, but it is not proven to be the root cause.

## Late-endpoint quarantine boot

The first boot with the 60-second guarded wait and quarantine path proved that
the endpoint delay can be substantially longer than the earlier 39-second
case:

| Monotonic time | Event |
| ---: | --- |
| `t=4.875s` | The expected Intel TH5P4 router is discovered and authorized. |
| `t=13.146s` | The boot service starts and detects TH5P4, but no RTX endpoint. |
| `t=74.444s` | The 60-second endpoint wait expires and SDDM is allowed to start on AMD. |
| `t=103.628s` | The root port finally reports `Card present` and `Link Up`. |
| `t=103.798s` | RTX `10de:2c05` and HDA `10de:22e9` enumerate. |
| `t=103.831s` | The udev-triggered quarantine service takes ownership of the late endpoint. |
| `t=104.136s` | GPU and downstream bridge are verified at fixed Gen3 x4; NVIDIA and HDA remain unbound. |

This boot remained responsive on AMD and produced the pending-hot-attach
marker. It validates the quarantine design, but also proves that a synchronous
wait cannot provide both a fast login and reliable endpoint capture: the RTX
appeared about 90.5 seconds after the service began. The final policy therefore
uses only a five-second guarded wait. A later endpoint is staged and loaded
asynchronously without terminating the running AMD session; the generated
NVIDIA-first KWin order takes effect at the user's next voluntary login.

## Fast-policy successful boot

The first controlled boot with the five-second/async policy took the normal
early-endpoint path:

| Monotonic time | Event |
| ---: | --- |
| `t=1.931s` | RTX and HDA endpoints are already enumerated. |
| `t=4.611s` | TH5P4 router discovery completes. |
| `t=16.019s` | The boot service starts with the endpoint immediately available. |
| `t=16.201s` | Bridge and GPU settle at fixed Gen3 x4. |
| `t=18.311s` | NVIDIA DRM initializes. |
| `t=18.464s` | NVIDIA, late-bound HDA, and NVIDIA-first KWin order are ready. |

No synchronous endpoint wait was entered. OpenGL/KWin use the RTX, external DP
and HDMI plus the internal AMD eDP are connected, and the boot contains no Xid,
AER, PCIe bus error, amdgpu reset, or pageflip-timeout signature.

## Installer changes to make next

1. Replace the fixed 8-second wait with two-stage discovery:
   - exit quickly when no expected TH5P4 USB4 router is attached;
   - when the router is present, block the display manager for at most five seconds while waiting for an already-near endpoint.
2. Add a udev/systemd path for late PCIe enumeration so an RTX that misses the boot window is staged and loaded asynchronously without forcing a logout.
3. Keep `03:00.1` unbound until Gen3/D0 staging and NVIDIA DRM initialization finish.
4. Record a small per-boot diagnostic state file containing router discovery, PCI enumeration, link speed, driver-load order, and KWin GPU order.
5. Consider inhibiting suspend while the eGPU is the primary KWin device, or implement an explicit eGPU-aware suspend path.
6. Test three paths separately before any Gen4 experiment: eGPU present early, eGPU present late, and eGPU absent.

## Raw journal retrieval

```bash
journalctl --boot=dec99addb5a34b1f934688cb40de417a -o short-monotonic
journalctl --boot=45b352305f854a8ba78c9f10bcfb360a -o short-monotonic
```

These journals are currently stored persistently on the host. Preserve this report if journal vacuuming is performed.
