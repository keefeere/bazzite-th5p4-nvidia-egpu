#!/usr/bin/env python3
"""Control the Cardwire service-roles owner (needs a cardwired built with it).

status                      generation, current profile, live masks, profiles
reconcile --role-unit ...   enroll roles to their services' CURRENT cgroup and admit the
                            matching running processes (root; --loop for a service)
apply PROFILE               apply a configured profile; verify readback; on a
                            failed readback restore the previous profile
enroll INDEX UNIT [--exe P] re-enroll role INDEX after UNIT restarted (new
                            cgroup). Usable as a root ExecStartPre=+ of the unit:
                            pass --exe because the process has not exec'd yet.
Never switches GPU mode, restarts services or touches the session; the
D-Bus methods are root-only, so run via sudo/pkexec or a root unit step.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

SERVICE = "org.opengamingcollective.cardwire"
OBJECT = "/org/opengamingcollective/cardwire"
IFACE = SERVICE + ".ServiceRoles"
NAME = re.compile(r"[a-z0-9_-]{1,32}")
UNIT = re.compile(r"[A-Za-z0-9_.@:-]+\.service")


def run(args):
    return subprocess.run(args, text=True, capture_output=True, timeout=30)


def busctl(runner, *args):
    result = runner(["busctl", "--system", *args])
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "busctl failed")
    return result.stdout.strip()


def get_property(runner, name):
    return busctl(runner, "get-property", SERVICE, OBJECT, IFACE, name)


def parse_value(text):
    """Parse the busctl subset used here: t N | s "x" | au N ... | as N "a" ..."""
    kind, _, rest = text.partition(" ")
    if kind in ("t", "u"):
        return int(rest)
    if kind == "s":
        return rest.strip().strip('"')
    if kind == "au":
        numbers = rest.split()
        if int(numbers[0]) != len(numbers) - 1:
            raise ValueError("malformed array")
        return [int(n) for n in numbers[1:]]
    if kind == "as":
        items = re.findall(r'"([^"]*)"', rest)
        if int(rest.split()[0]) != len(items):
            raise ValueError("malformed array")
        return items
    raise ValueError(f"unsupported busctl value: {text!r}")


def status(runner=run):
    return {
        "generation": parse_value(get_property(runner, "Generation")),
        "current_profile": parse_value(get_property(runner, "CurrentProfile")),
        "permissions": parse_value(get_property(runner, "Permissions")),
        "default_mask": parse_value(get_property(runner, "DefaultMask")),
        "profiles": parse_value(get_property(runner, "Profiles")),
    }


AMD_VENDOR = "0x1002"
LAST_ERROR = Path("/run/egpu-service-roles-last-error")
DESIRED = Path("/var/lib/egpu-nvidia-service-roles/desired-profile")
KWIN_SPEC = {"scope": "user", "unit": "plasma-kwin_wayland.service", "uid": 1000}


def kwin_primary_vendor(cgroup_root=Path("/sys/fs/cgroup"), proc=Path("/proc"), sysfs=Path("/sys")):
    """PCI vendor of the GPU the running KWin treats as primary (first KWIN_DRM_DEVICES entry).
    Returns None when KWin or the variable cannot be found."""
    directory = unit_cgroup_dir(KWIN_SPEC, cgroup_root)
    if directory is None:
        return None
    for pid in (directory / "cgroup.procs").read_text().split():
        try:
            environ = (proc / pid / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        for item in environ:
            if item.startswith(b"KWIN_DRM_DEVICES="):
                first = item.split(b"=", 1)[1].decode().split(":")[0]
                card = Path(first).name
                if not re.fullmatch(r"card\d+", card):
                    return None
                try:
                    return (sysfs / "class/drm" / card / "device/vendor").read_text().strip()
                except OSError:
                    return None
    return None


def preflight(name, vendor_reader=kwin_primary_vendor):
    """A work profile removes NVIDIA from every NEW process. That is only safe when the compositor
    renders on the AMD GPU; otherwise new windows (even this widget) cannot be drawn."""
    if name.startswith("work-"):
        vendor = vendor_reader()
        if vendor != AMD_VENDOR:
            raise RuntimeError(
                "Work profiles need KWin to render on the AMD GPU, but it renders on NVIDIA now "
                "(new programs and this widget would stop drawing). Refusing; switch KWin to "
                "AMD-first first (needs a session restart).")


def apply_profile(name, runner=run, force=False, vendor_reader=None):
    if not NAME.fullmatch(name):
        raise ValueError("invalid profile name")
    if not force:
        preflight(name, vendor_reader or kwin_primary_vendor)
    before = status(runner)
    if name not in before["profiles"]:
        raise ValueError(f"unknown profile {name}; configured: {before['profiles']}")
    busctl(runner, "call", SERVICE, OBJECT, IFACE, "ApplyProfile", "s", name)
    after = status(runner)
    if after["current_profile"] == name:
        return {"applied": name, "previous": before["current_profile"], "status": after}
    restored = None
    if before["current_profile"]:
        try:
            busctl(runner, "call", SERVICE, OBJECT, IFACE, "ApplyProfile", "s",
                   before["current_profile"])
            restored = status(runner)["current_profile"] == before["current_profile"]
        except RuntimeError:
            restored = False
    raise RuntimeError(f"readback is {after['current_profile']!r}, wanted {name!r}; "
                       f"previous profile restored: {restored}")


def unit_cgroup(unit, runner=run, root=Path("/sys/fs/cgroup")):
    if not UNIT.fullmatch(unit):
        raise ValueError("invalid unit name")
    out = runner(["systemctl", "show", unit, "-p", "ControlGroup", "--value"])
    group = out.stdout.strip()
    if out.returncode or not group.startswith("/") or ".." in group.split("/"):
        raise RuntimeError("unit has no control group (not started?)")
    path = root / group.lstrip("/")
    if not path.is_dir():
        raise RuntimeError(f"cgroup directory missing: {path}")
    return str(path)


def unit_executable(unit, runner=run, proc=Path("/proc")):
    out = runner(["systemctl", "show", unit, "-p", "MainPID", "--value"])
    pid = out.stdout.strip()
    if out.returncode or not pid.isdigit() or pid == "0":
        raise RuntimeError("unit has no main process; pass --exe")
    return os.readlink(proc / pid / "exe")


def enroll(index, unit, exe=None, runner=run, cgroup_root=Path("/sys/fs/cgroup")):
    if not 0 <= index < 16:
        raise ValueError("role index out of range")
    if exe is not None and (not exe.startswith("/") or ".." in exe.split("/")):
        raise ValueError("absolute executable path required")
    cgroup = unit_cgroup(unit, runner, cgroup_root)
    executable = exe or unit_executable(unit, runner)
    out = busctl(runner, "call", SERVICE, OBJECT, IFACE, "ReEnrollRole", "uss",
                 str(index), executable, cgroup)
    return {"generation": parse_value(out), "executable": executable, "cgroup": cgroup}


# ---- reconcile: keep configured roles bound to their CURRENT service identity ----

ROLE_UNIT = re.compile(r"(\d{1,2}):(user|system):([A-Za-z0-9_.@:-]+\.service):(/[^\x00:]+):(\d+)")


def parse_role_unit(text):
    """INDEX:SCOPE:UNIT:EXE:UID, e.g. 1:user:plasma-kwin_wayland.service:/usr/bin/kwin_wayland:1000"""
    match = ROLE_UNIT.fullmatch(text)
    if not match or ".." in match.group(4).split("/") or int(match.group(1)) >= 16:
        raise ValueError(f"invalid role unit {text!r}")
    index, scope, unit, exe, uid = match.groups()
    return {"index": int(index), "scope": scope, "unit": unit, "exe": exe, "uid": int(uid)}


def parse_roles(text):
    """a(tttu) -> [(incarnation, cgroup_id, exe_inode, uid)]"""
    kind, _, rest = text.partition(" ")
    if kind != "a(tttu)":
        raise ValueError(f"unexpected Roles type {kind!r}")
    numbers = [int(n) for n in rest.split()]
    if not numbers or len(numbers) != 1 + 4 * numbers[0]:
        raise ValueError("malformed Roles array")
    return [tuple(numbers[1 + 4 * i: 5 + 4 * i]) for i in range(numbers[0])]


def unit_cgroup_dir(spec, cgroup_root=Path("/sys/fs/cgroup")):
    """Current cgroup directory of a unit, found in the cgroup tree (no sessions, no PAM).
    System units live in system.slice; user units under the user manager's subtree."""
    unit = spec["unit"]
    if spec["scope"] == "system":
        candidates = [cgroup_root / "system.slice" / unit]
    else:
        uid = spec["uid"]
        manager = cgroup_root / f"user.slice/user-{uid}.slice/user@{uid}.service"
        candidates = sorted(manager.glob(f"*/{unit}")) if manager.is_dir() else []
    candidates = [c for c in candidates if c.is_dir()]
    if len(candidates) > 1:
        raise RuntimeError(f"{unit}: ambiguous cgroup {candidates}")
    return candidates[0] if candidates else None


def process_start(pid, proc=Path("/proc")):
    """starttime field of /proc/PID/stat (identifies a process across PID reuse)."""
    text = (proc / str(pid) / "stat").read_text()
    return text.rsplit(")", 1)[1].split()[19]


def reconcile_role(spec, roles, admitted, runner=run, cgroup_root=Path("/sys/fs/cgroup"),
                   proc=Path("/proc")):
    """One role: enroll when the service has a new cgroup, admit its matching running
    processes. Returns a list of human-readable actions (empty when already in sync)."""
    actions = []
    directory = unit_cgroup_dir(spec, cgroup_root)
    if directory is None:
        return [f"{spec['unit']}: not running"]
    index = spec["index"]
    if index >= len(roles):
        raise RuntimeError(f"role {index} is not in the catalog")
    if roles[index][1] != directory.stat().st_ino:
        result = enroll_paths(index, spec["exe"], str(directory), runner)
        actions.append(f"{spec['unit']}: enrolled role {index} (generation {result})")
    exe = os.path.realpath(spec["exe"])
    for pid in (directory / "cgroup.procs").read_text().split():
        try:
            if os.path.realpath(os.readlink(proc / pid / "exe")) != exe:
                continue
            key = (pid, process_start(pid, proc))
        except OSError:
            continue
        if key in admitted:
            continue
        try:
            busctl(runner, "call", SERVICE, OBJECT, IFACE, "AdmitProcess", "uu", pid, str(index))
            admitted.add(key)
            actions.append(f"{spec['unit']}: admitted pid {pid}")
        except RuntimeError as error:
            actions.append(f"{spec['unit']}: pid {pid} not admitted: {error}")
    return actions


def enroll_paths(index, exe, cgroup, runner=run):
    out = busctl(runner, "call", SERVICE, OBJECT, IFACE, "ReEnrollRole", "uss", str(index), exe, cgroup)
    return parse_value(out)


def all_bound(specs, roles, cgroup_root=Path("/sys/fs/cgroup")):
    """Every configured role is bound to its service's CURRENT cgroup."""
    for spec in specs:
        directory = unit_cgroup_dir(spec, cgroup_root)
        if directory is None or spec["index"] >= len(roles) or roles[spec["index"]][1] != directory.stat().st_ino:
            return False
    return True


def restore_desired(specs, state, runner=run, desired_path=None, cgroup_root=Path("/sys/fs/cgroup"),
                    vendor_reader=kwin_primary_vendor):
    """Re-apply the remembered profile after a daemon/boot reset, but only once every role is bound
    and (for work profiles) the compositor renders on AMD. Returns a message or None."""
    path = desired_path or DESIRED
    try:
        desired = path.read_text().strip()
    except OSError:
        return None
    if not NAME.fullmatch(desired):
        return None
    info = status(runner)
    if info["current_profile"] == desired or desired not in info["profiles"]:
        return None
    if not all_bound(specs, parse_roles(get_property(runner, "Roles")), cgroup_root):
        return None
    try:
        apply_profile(desired, runner, vendor_reader=vendor_reader)
    except RuntimeError as error:
        text = f"remembered profile {desired} not restored: {error}"
        if state.get("last_refusal") == text:
            return None  # say it once, not every pass
        state["last_refusal"] = text
        return text
    state.pop("last_refusal", None)
    return f"restored remembered profile {desired}"


def reconcile(specs, admitted, runner=run, **kwargs):
    roles = parse_roles(get_property(runner, "Roles"))
    actions = []
    for spec in specs:
        actions += reconcile_role(spec, roles, admitted, runner, **kwargs)
        roles = parse_roles(get_property(runner, "Roles"))  # enrollment changes identities
    return actions


def note_error(message):
    """Reason of the last refused/failed apply, readable by the widget (best effort)."""
    try:
        if message is None:
            LAST_ERROR.unlink(missing_ok=True)
        else:
            LAST_ERROR.write_text(message + "\n")
            os.chmod(LAST_ERROR, 0o644)
    except OSError:
        pass


def remember_profile(name):
    """The profile to restore after the next boot (applied by the reconciler when safe)."""
    try:
        DESIRED.parent.mkdir(parents=True, exist_ok=True)
        DESIRED.write_text(name + "\n")
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p = sub.add_parser("apply")
    p.add_argument("profile")
    p.add_argument("--force", action="store_true", help="skip the KWin-on-AMD safety check")
    p = sub.add_parser("reconcile")
    p.add_argument("--role-unit", action="append", required=True, type=parse_role_unit)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=float, default=5.0)
    p = sub.add_parser("enroll")
    p.add_argument("index", type=int)
    p.add_argument("unit")
    p.add_argument("--exe")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result = status()
        elif args.command == "reconcile":
            admitted = set()
            state = {}
            while True:
                try:
                    actions = reconcile(args.role_unit, admitted)
                    for line in actions:
                        print(line, flush=True)
                    if args.loop and not actions:  # a stable pass: roles are in sync
                        message = restore_desired(args.role_unit, state)
                        if message:
                            print(message, flush=True)
                except (RuntimeError, ValueError, OSError) as error:
                    if not args.loop:
                        raise
                    print(f"reconcile: {error}", file=sys.stderr, flush=True)
                if not args.loop:
                    return 0
                time.sleep(max(1.0, args.interval))
        elif args.command == "apply":
            try:
                result = apply_profile(args.profile, force=args.force)
            except (RuntimeError, ValueError) as error:
                note_error(str(error))
                raise
            note_error(None)
            remember_profile(args.profile)
        else:
            result = enroll(args.index, args.unit, args.exe)
    except (RuntimeError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    import json
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
