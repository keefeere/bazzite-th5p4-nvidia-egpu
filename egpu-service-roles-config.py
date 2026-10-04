#!/usr/bin/env python3
"""Read-only generator for Cardwire's /etc/cardwire/service-roles.toml.

Inventories the NVIDIA device nodes that actually exist (DRM nodes found via the
PCI identity, never fixed card numbers) and maps the three profiles to per-role
masks. Prints TOML to stdout; installs nothing and never talks to Cardwire.
Roles are explicit narrow services: --role KIND:EXECUTABLE:CGROUP:UID with KIND
compute|display. The profile->device mapping is a starting policy that must be
verified on hardware (notably the display role's render-node need in Work/NVIDIA).
"""

import argparse
import importlib.util
import os
from pathlib import Path
import stat
import sys

PROFILES = ("gaming-nvidia", "work-nvidia", "work-igpu")
KINDS = ("compute", "display")
MAX_DEVICES = 16
MAX_ROLES = 16
STATIC_NODES = {
    "nvidiactl": {"compute", "display"},
    "nvidia-uvm": {"compute"},
    "nvidia-uvm-tools": {"compute"},
    "nvidia-modeset": {"display"},
}
# Allowed device tags per (profile, role kind). Gaming keeps full capability.
ALLOWED = {
    ("gaming-nvidia", "compute"): {"compute", "display", "render"},
    ("gaming-nvidia", "display"): {"compute", "display", "render"},
    ("work-nvidia", "compute"): {"compute"},
    ("work-nvidia", "display"): {"display", "render"},
    ("work-igpu", "compute"): {"compute"},
    ("work-igpu", "display"): set(),
}


def load_planner():
    spec = importlib.util.spec_from_file_location(
        "profile_plan", Path(__file__).with_name("egpu-desktop-profile-plan.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def is_char_device(path):
    try:
        return stat.S_ISCHR(os.stat(path, follow_symlinks=False).st_mode)
    except OSError:
        return False


def inventory(vendor, device_id, sysfs=Path("/sys"), dev=Path("/dev")):
    """Return ordered [(path, tags)] of existing NVIDIA nodes for one PCI identity."""
    nodes = []
    pci = sysfs / "bus/pci/devices"
    for device in sorted(pci.iterdir()):
        read = lambda name: (device / name).read_text().strip() if (device / name).exists() else None
        if read("vendor") != vendor or read("device") != device_id:
            continue
        drm = device / "drm"
        for entry in sorted(drm.iterdir()) if drm.is_dir() else []:
            if entry.name.startswith("card") and entry.name[4:].isdigit():
                nodes.append((str(dev / "dri" / entry.name), {"display"}))
            elif entry.name.startswith("renderD") and entry.name[7:].isdigit():
                nodes.append((str(dev / "dri" / entry.name), {"render"}))
    for entry in sorted(dev.glob("nvidia[0-9]*")):
        if entry.name[6:].isdigit():
            nodes.append((str(entry), {"compute", "display"}))
    for name, tags in STATIC_NODES.items():
        nodes.append((str(dev / name), set(tags)))
    nodes = [(p, t) for p, t in nodes if is_char_device(p)]
    if not nodes:
        raise ValueError("no NVIDIA device nodes found")
    return nodes[:MAX_DEVICES]


def parse_role(text):
    parts = text.split(":")
    if len(parts) != 4 or parts[0] not in KINDS or not parts[3].isdigit():
        raise ValueError(f"role must be KIND:EXECUTABLE:CGROUP:UID: {text}")
    kind, exe, cgroup, uid = parts
    exe_path = Path(exe).resolve()
    if not exe_path.is_file() or not os.access(exe_path, os.X_OK):
        raise ValueError(f"executable not found: {exe}")
    cg = Path(cgroup)
    if not cg.is_absolute() or not cg.is_dir() or ".." in cg.parts:
        raise ValueError(f"cgroup directory not found: {cgroup}")
    return kind, str(exe_path), str(cg), int(uid)


def masks(profile, roles, nodes):
    result = []
    for kind, *_ in roles:
        allowed = ALLOWED[(profile, kind)]
        result.append(sum(1 << n for n, (_, tags) in enumerate(nodes) if tags & allowed))
    return result


def default_mask(profile, nodes):
    """Gaming keeps ordinary NVIDIA access for every process; Work is roles-only."""
    return (1 << len(nodes)) - 1 if profile == "gaming-nvidia" else 0


def render(nodes, roles, bpf_object, unit="cardwired.service", initial_profile=None):
    if not 1 <= len(roles) <= MAX_ROLES:
        raise ValueError("1..16 roles required")
    for text in [bpf_object, unit] + [p for p, _ in nodes] + [r[i] for r in roles for i in (1, 2)]:
        if any(c in text for c in '"\\\n\r'):
            raise ValueError(f"unsafe character in {text!r}")
    if initial_profile is not None and initial_profile not in PROFILES:
        raise ValueError("unknown initial profile")
    out = ["enabled = true"]
    if initial_profile:
        out.append(f'initial_profile = "{initial_profile}"')
    out += [f'bpf_object = "{bpf_object}"', f'unit = "{unit}"', "devices = ["]
    out += [f'  "{p}",' for p, _ in nodes] + ["]", ""]
    for kind, exe, cgroup, uid in roles:
        out += [f"# {kind} role", "[[role]]", f'executable = "{exe}"',
                f'cgroup = "{cgroup}"', f"uid = {uid}", ""]
    for profile in PROFILES:
        out += ["[[profile]]", f'name = "{profile}"',
                f"permissions = {masks(profile, roles, nodes)}",
                f"default_mask = {default_mask(profile, nodes)}", ""]
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hardware-config", type=Path, required=True)
    parser.add_argument("--role", action="append", required=True)
    parser.add_argument("--bpf-object", default="/usr/lib/cardwire/service_guard.bpf.o")
    args = parser.parse_args(argv)
    try:
        planner = load_planner()
        config = planner.read_hardware_config(args.hardware_config)
        nodes = inventory(config["EGPU_VENDOR"], config["EGPU_DEVICE"])
        roles = [parse_role(r) for r in args.role]
        print(render(nodes, roles, args.bpf_object))
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
