#!/usr/bin/env python3
"""Control the Cardwire service-roles owner (needs a cardwired built with it).

status                      generation, current profile, live masks, profiles
apply PROFILE               apply a configured profile; verify readback; on a
                            failed readback restore the previous profile
enroll INDEX UNIT [--exe P] re-enroll role INDEX after UNIT restarted (new
                            cgroup). Usable as a root ExecStartPre=+ of the unit:
                            pass --exe because the process has not exec'd yet.
Never switches GPU mode, restarts services or touches the session; the
D-Bus methods are root-only, so run via sudo/pkexec or a root unit step.
"""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

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


def apply_profile(name, runner=run):
    if not NAME.fullmatch(name):
        raise ValueError("invalid profile name")
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p = sub.add_parser("apply")
    p.add_argument("profile")
    p = sub.add_parser("enroll")
    p.add_argument("index", type=int)
    p.add_argument("unit")
    p.add_argument("--exe")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result = status()
        elif args.command == "apply":
            result = apply_profile(args.profile)
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
