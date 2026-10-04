#!/usr/bin/env python3
"""Exercise Cardwire process-policy API on our temporary sleep children only.

Requires Hybrid mode. Does not change Mode, configuration, application policy,
GPU state or any other process's permissions. This is not a GPU rendering test.
"""
import argparse
import json
import os
import signal
import subprocess
import sys

SERVICE = "org.opengamingcollective.cardwire"
OBJECT = "/org/opengamingcollective/cardwire"


def command(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=5)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout


def probe(runner=command, spawner=subprocess.Popen, *, exact_policy=False):
    mode = json.loads(runner(["busctl", "--json=short", "get-property", SERVICE,
                              OBJECT, SERVICE + ".Mode", "Mode"]))
    if mode.get("data") != 1:
        raise RuntimeError("Hybrid mode is required; the probe will not change it")
    api = ["busctl", "--json=short", "call", SERVICE, OBJECT, SERVICE + ".SmartPolicy"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CARDWIRE_")}
    checked = []
    initials = ["Allow_dGPU", "Force_dGPU", "Force_GPU"]
    if exact_policy:
        initials.append("Allow_dGPU_Exact")
    for initial in initials:
        with spawner(["/usr/bin/sleep", "60"], env=env) as child:
            try:
                def status():
                    return json.loads(runner(api + ["GetProcessStatus", "u", str(child.pid)]))["data"]

                if status() != ["", []]:
                    raise RuntimeError("Diagnostic child is already classified; refusing ambiguous test")
                requests = [(initial, 1), (initial, 1), ("Allow_dGPU", 1),
                            ("Force_dGPU", 0), ("Force_GPU", 1),
                            ("Allow_dGPU", 1)]
                if exact_policy:
                    # Opt-in compatibility probe for the maintained fork; an
                    # old daemon must fail, never fall back to inheritable Allow.
                    requests += [("Allow_dGPU_Exact", 1), ("Allow_dGPU_Exact", 1),
                                 ("Force_GPU", 0), ("Allow_dGPU_Exact", 1),
                                 ("Allow_dGPU", 1), ("Allow_dGPU_Exact", 1)]
                requests.append(("Default", 0))
                expected = ["", []]
                for policy, value in requests:
                    runner(api + ["RequestProcessAccess", "usu", str(child.pid), policy, str(value)])
                    if policy != "Default":
                        expected = (["AllowedExact", [0]] if policy == "Allow_dGPU_Exact"
                                    else ["Allowed", [0]] if policy == "Allow_dGPU"
                                    else ["Forced", [value]])
                    actual = status()
                    if actual != expected:
                        raise RuntimeError(f"{initial}/{policy}: readback {actual!r}, expected {expected!r}")
                    checked.append({"initial": initial, "policy": policy, "value": value})
            finally:
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
    return {"status": "passed", "checks": checked, "count": len(checked),
            "exact_policy_readback_verified": exact_policy,
            "inheritance_enforcement_verified": False,
            "scope": "Own diagnostic children only; no rendering or isolation claim"}


def interrupted(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exact-policy', action='store_true',
                        help='Also require the fork\'s exact-process API; no legacy fallback, not an inheritance test')
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        print(json.dumps(probe(exact_policy=args.exact_policy), indent=2))
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired,
            KeyboardInterrupt) as error:
        print(json.dumps({"status": "failed", "error": str(error)}))
        sys.exit(1)
