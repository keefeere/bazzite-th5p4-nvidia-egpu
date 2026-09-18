#!/usr/bin/python3
"""One explicit freezer/devices/platform eGPU PM test; never launched by check.

Run on the host as root. No permanent unit, module option or kernel argument is
installed. A copied worker in a transient system service owns the experiment.
"""

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time


LOG_ROOT = Path("/var/log/egpu-sleep-lab")
LOCK = Path("/run/egpu-sleep-lab.lock")
SLEEP_CONF = Path("/run/systemd/sleep.conf.d/zzzz-egpu-sleep-lab.conf")
UNIT_CONF = Path("/run/systemd/system/systemd-suspend.service.d/zzzz-egpu-sleep-lab.conf")
PM_TEST = Path("/sys/power/pm_test")
SLEEP_GUARD = Path("/etc/egpu-nvidia/egpu-sleep-guard.sh")
SLEEP_GUARD_ONCE = Path("/run/egpu-sleep-guard-platform-once")
HOST_RESET = Path("/sys/module/thunderbolt/parameters/host_reset")
CMDLINE = Path("/proc/cmdline")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
DEPTH = Path("/proc/driver/nvidia/suspend_depth")
FLAGS = (Path("/sys/power/pm_debug_messages"), Path("/sys/power/pm_print_times"))
SLEEP_UNITS = ("systemd-suspend.service", "systemd-hibernate.service",
               "systemd-hybrid-sleep.service", "systemd-suspend-then-hibernate.service")
EGPU_UNITS = ("egpu-nvidia-detach.service", "egpu-nvidia-hot-attach.service",
              "egpu-nvidia-quarantine.service")
LOGIN1 = ("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager")
# 0x01 = SD_LOGIND_ROOT_CHECK_INHIBITORS. Do not use SKIP_INHIBITORS (0x10).
SUSPEND_REQUEST = ["busctl", "--system", "--allow-interactive-authorization=no",
                   "call", *LOGIN1, "SuspendWithFlags", "t", "1"]
TEST_STAGES = ("freezer", "devices", "platform")


def validate_stage(stage):
    if stage not in TEST_STAGES:
        raise RuntimeError("Only explicit freezer/devices/platform tests are supported; no full sleep.")
    return stage


def command(argv, timeout=10):
    return subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout, check=False)


def require_command(argv):
    result = command(argv)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} failed ({result.returncode}): {result.stdout.strip()}")
    return result.stdout


def selected(text):
    match = re.search(r"\[([^]]+)\]", text)
    if not match:
        raise RuntimeError(f"Cannot resolve active setting: {text!r}")
    return match.group(1)


def parse_compute_clients(text):
    """Return GPU compute/UVM clients other than the compositor."""
    clients = []
    for line in text.splitlines():
        if not line.strip() or "No running processes found" in line:
            continue
        pid, separator, name = line.partition(",")
        if not separator or not pid.strip().isdigit():
            raise RuntimeError(f"Cannot parse NVIDIA compute client: {line!r}")
        name = name.strip()
        if Path(name).name != "kwin_wayland":
            clients.append((pid.strip(), name))
    return clients


def unit_state(unit):
    output = require_command(["systemctl", "show", unit, "-p", "ActiveState",
                              "-p", "SubState", "-p", "ExecMainStartTimestampMonotonic",
                              "-p", "Result", "-p", "MainPID"])
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def is_busy(state):
    return state.get("ActiveState") not in ("inactive", "failed")


def assert_sleep_idle(baseline=None):
    if require_command(["busctl", "--system", "get-property", *LOGIN1,
                        "PreparingForSleep"]).strip() != "b false":
        raise RuntimeError("logind is preparing for sleep; cannot restore settings.")
    targets = ("sleep.target", "suspend.target", "hibernate.target", "hybrid-sleep.target",
               "suspend-then-hibernate.target")
    for unit in SLEEP_UNITS + targets:
        current = unit_state(unit)
        if is_busy(current):
            raise RuntimeError(f"Sleep transition still active: {unit}")
        if unit == "systemd-suspend.service" and baseline is not None:
            if current.get("ExecMainStartTimestampMonotonic") != baseline:
                raise RuntimeError("A new sleep invocation occurred; manual inspection required.")
    jobs = require_command(["systemctl", "list-jobs", "--no-legend", "--no-pager"])
    if any(word in SLEEP_UNITS + targets for word in jobs.split()):
        raise RuntimeError("A sleep job is queued; cannot restore settings.")


def definitive_rejection(output):
    return ("Operation denied due to active block inhibitor" in output
            or "Please retry operation after closing inhibitors and logging out other users." in output)


def unit_override(folder):
    return ("[Service]\nEnvironment=SYSTEMD_LOG_LEVEL=debug\n"
            f"ExecStopPost=/usr/bin/python3 {folder / 'worker.py'} _complete\n")


def wait_for_cycle(baseline, timeout=120, completion=None):
    """Wait for a NEW invocation to end; enqueue success is not cycle success."""
    observed = False
    deadline = time.monotonic() + timeout
    while True:
        state = unit_state("systemd-suspend.service")
        stamp = state.get("ExecMainStartTimestampMonotonic", "0")
        observed |= stamp.isdigit() and int(stamp) > 0 and stamp != baseline
        # systemd may garbage-collect an inactive oneshot before our next poll.
        # A run-specific ExecStopPost record survives that, including failures.
        if completion is not None and completion.exists() and not is_busy(state):
            recorded = json.loads(completion.read_text())
            if recorded.get("service_result"):
                return {**state, "Result": recorded["service_result"], "Completion": "ExecStopPost"}
        if observed and not is_busy(state):
            return state
        if time.monotonic() >= deadline:
            raise RuntimeError("Cycle did not finish within the deadline. Temporary settings retained; no second request issued.")
        time.sleep(0.25)


def freezer_confirmed(state, log):
    return stage_confirmed("freezer", state, log)


def stage_confirmed(stage, state, log):
    validate_stage(stage)
    # A completed service alone can also mean an aborted or rejected PM test.
    common = (state.get("Result") == "success"
            and len(re.findall(r"PM: suspend entry", log)) == 1
            and len(re.findall(r"PM: suspend exit", log)) == 1
            and "suspend debug: Waiting for" in log
            and "Freezing user space processes" in log
            and not re.search(r"Freezing .* (?:failed|aborted)", log))
    if stage == "freezer":
        return common and "PM: suspend devices took" not in log and "Suspending console(s)" not in log
    # TEST_DEVICES stops after dpm_suspend_start, before late/noirq/platform.
    suspend = log.find("PM: suspend devices took")
    pause = log.find("suspend debug: Waiting for")
    resume = log.find("PM: resume devices took")
    if stage == "devices":
        return (common and 0 <= suspend < pause < resume
                and "late suspend of devices" not in log
                and "noirq suspend of devices" not in log)
    # TEST_PLATFORM completes ordinary, late, and noirq device suspension plus
    # s2idle platform preparation. It returns before s2idle_loop() can wait for
    # a real wake event, then unwinds noirq/early/ordinary resume in order.
    markers = ("PM: suspend devices took",
               "PM: late suspend of devices complete",
               "PM: noirq suspend of devices complete",
               "suspend debug: Waiting for",
               "PM: noirq resume of devices complete",
               "PM: early resume of devices complete",
               "PM: resume devices took")
    positions = [log.find(marker) for marker in markers]
    return (common and all(position >= 0 for position in positions)
            and positions == sorted(positions)
            and "suspend-to-idle" not in log
            and "resume from suspend-to-idle" not in log)


def effective_suspend_states(config):
    values = []
    section = ""
    for line in config.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line
        elif section == "[Sleep]" and "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
            if key == "SuspendState":
                if not value:
                    values = []
                else:
                    values.extend(value.split())
    return values or ["mem", "standby", "freeze"]


def exact_cmdline_argument(name, value):
    tokens = CMDLINE.read_text().split()
    named = [token for token in tokens if token.split("=", 1)[0] == name]
    expected = f"{name}={value}"
    if named != [expected]:
        raise RuntimeError(f"Expected exactly one {expected} kernel argument; found {named or 'none'}.")


def validate_guard_bypass(stage):
    if stage != "platform":
        raise RuntimeError("The sleep-guard bypass is valid only for the explicit platform test.")
    for name, value in (("thunderbolt.host_reset", "0"),
                        ("egpu.host_reset_test", "1"),
                        ("egpu.host_reset_nodock", "1")):
        exact_cmdline_argument(name, value)
    if HOST_RESET.read_text().strip() != "N":
        raise RuntimeError("The live Thunderbolt host_reset value is not N.")
    if not SLEEP_GUARD.is_file() or SLEEP_GUARD.is_symlink():
        raise RuntimeError("The installed eGPU sleep guard is missing or is a symlink.")
    exec_pre = require_command(["systemctl", "show", "systemd-suspend.service",
                                "-p", "ExecStartPre", "--value"])
    if f"{SLEEP_GUARD} systemd-suspend.service" not in exec_pre:
        raise RuntimeError("Install the updated, unit-scoped eGPU sleep guard before this diagnostic.")
    deployment = json.loads(require_command(["rpm-ostree", "status", "--json"]))
    if (deployment.get("transaction") is not None or not deployment.get("deployments")
            or any(item.get("staged", False) for item in deployment["deployments"])):
        raise RuntimeError("Normal next-boot recovery is not finalized; do not test sleep.")
    next_args = require_command(["rpm-ostree", "kargs"]).split()
    if not next_args or any(token.split("=", 1)[0] in
                           ("thunderbolt.host_reset", "egpu.host_reset_test", "egpu.host_reset_nodock")
                           for token in next_args):
        raise RuntimeError("Next-boot kernel arguments still contain the host-reset experiment.")
    if SLEEP_GUARD_ONCE.exists() or SLEEP_GUARD_ONCE.is_symlink():
        raise RuntimeError(f"A stale one-shot sleep marker exists: {SLEEP_GUARD_ONCE}")


def arm_guard_platform_once(folder):
    validate_guard_bypass("platform")
    if selected(PM_TEST.read_text()) != "platform":
        raise RuntimeError("Refusing to arm an exception without pm_test=platform.")
    boot_id = BOOT_ID.read_text().strip()
    payload = f"{boot_id}\nplatform\n".encode()
    descriptor = os.open(SLEEP_GUARD_ONCE,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if Path("/sys/fs/selinux/enforce").exists():
            require_command(["restorecon", "-F", str(SLEEP_GUARD_ONCE)])
            require_command(["matchpathcon", "-V", str(SLEEP_GUARD_ONCE)])
        status = SLEEP_GUARD_ONCE.stat()
        if status.st_uid != 0 or status.st_mode & 0o777 != 0o600:
            raise RuntimeError("One-shot sleep marker ownership or mode is unsafe.")
        (folder / "sleep-guard-bypass.txt").write_text(
            "Armed one platform-only invocation for this boot; the guard consumes it before systemd-sleep.\n")
    except BaseException:
        SLEEP_GUARD_ONCE.unlink(missing_ok=True)
        raise


def preflight(stage="freezer", guard_bypass=False):
    validate_stage(stage)
    if os.geteuid() != 0:
        raise RuntimeError("Run check/run on the host with sudo, outside the agent sandbox.")
    if selected(PM_TEST.read_text()) != "none" or stage not in PM_TEST.read_text().split():
        raise RuntimeError(f"pm_test must be none, with {stage} supported.")
    if selected(Path("/sys/power/mem_sleep").read_text()) != "s2idle":
        raise RuntimeError("This experiment expects active s2idle.")
    if "default" not in DEPTH.read_text().split():
        raise RuntimeError("NVIDIA suspend_depth interface is unavailable.")
    params = Path("/proc/driver/nvidia/params").read_text()
    if not re.search(r"^UseKernelSuspendNotifiers:\s+1$", params, re.M):
        raise RuntimeError("This experiment requires NVIDIA kernel suspend notifiers.")
    interface = require_command(["busctl", "--system", "--xml-interface", "introspect", *LOGIN1])
    if '<method name="SuspendWithFlags">' not in interface:
        raise RuntimeError("logind SuspendWithFlags is unavailable; no unsafe fallback is used.")
    for unit in SLEEP_UNITS + EGPU_UNITS:
        if is_busy(unit_state(unit)):
            raise RuntimeError(f"A transition is already active: {unit}")
    assert_sleep_idle()
    for path in (SLEEP_CONF, UNIT_CONF):
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"A previous diagnostic override remains: {path}; reboot before retesting.")
    for path in FLAGS:
        if path.read_text().strip() not in ("0", "1"):
            raise RuntimeError(f"Unexpected value in {path}")
    gpu = require_command(["nvidia-smi", "--query-gpu=name,driver_version,pstate,memory.used,memory.total",
                           "--format=csv"])
    compute = require_command(["nvidia-smi", "--query-compute-apps=pid,process_name",
                               "--format=csv,noheader"])
    clients = parse_compute_clients(compute)
    if clients:
        summary = ", ".join(f"{pid}:{Path(name).name}" for pid, name in clients[:8])
        if len(clients) > 8:
            summary += f", +{len(clients) - 8} more"
        raise RuntimeError("Active NVIDIA compute/UVM clients would contaminate suspend: "
                           f"{summary}. Stop games, CUDA and Steam shader processing first.")
    if guard_bypass:
        validate_guard_bypass(stage)
    return {"kernel": os.uname().release,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "gpu": gpu.strip(), "pm_test": "none", "requested_stage": stage,
            "nvidia_depth_for_test": "default",
            "sleep_guard_bypass": "one platform invocation" if guard_bypass else "disabled",
            "warning": f"{stage} testing invokes NVIDIA PM notifiers and may hang the GPU or host."
                       + (" Devices will be suspended/resumed; stack capture may be frozen too."
                          if stage in ("devices", "platform") else "")
                       + (" Late/noirq and platform callbacks will run, but not the s2idle wait loop."
                          if stage == "platform" else "")}


def save_command(path, argv, timeout=10):
    # Diagnostic output is intentionally local and root-readable only.
    try:
        result = command(argv, timeout)
        output = result.stdout + f"\n[exit={result.returncode}]\n"
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or b""
        output = (partial.decode(errors="replace") if isinstance(partial, bytes) else partial)
        output += "\n[diagnostic command timed out]\n"
    except OSError as exc:
        output = f"[diagnostic unavailable: {exc}]\n"
    path.write_text(output)


def copy_text(source, target):
    try:
        target.write_text(source.read_text())
    except OSError as exc:
        target.write_text(f"[unavailable: {exc}]\n")


def snapshot(folder, phase):
    dest = folder / phase
    dest.mkdir(mode=0o700, exist_ok=True)
    for name, path in (("nvidia-params", "/proc/driver/nvidia/params"),
                       ("acpi-wakeup", "/proc/acpi/wakeup"),
                       ("nvidia-power", "/proc/driver/nvidia/gpus/0000:03:00.0/power"),
                       ("meminfo", "/proc/meminfo"),
                       ("pm-wakeup-irq", "/sys/power/pm_wakeup_irq"),
                       ("wakeup-sources", "/sys/kernel/debug/wakeup_sources")):
        copy_text(Path(path), dest / (name + ".txt"))
    for path in Path("/sys/power/suspend_stats").glob("*"):
        if path.is_file():
            copy_text(path, dest / ("pm-stat-" + path.name + ".txt"))
    save_command(dest / "processes.txt", ["ps", "-eo", "pid,ppid,stat,wchan:40,comm"])
    save_command(dest / "sleep-unit.txt", ["systemctl", "show", "systemd-suspend.service"])
    save_command(dest / "kernel.txt", ["journalctl", "-k", "-b", "--no-pager", "-n", "180",
                                        "-o", "short-monotonic"])


def dump_stacks(folder, index):
    # Read tasks directly; avoid GPU ioctls/NVML polling during the transition.
    chunks = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            name = (proc / "comm").read_text().strip()
            state = (proc / "status").read_text()
            blocked = re.search(r"^State:\s+D\b", state, re.M)
            if not blocked and not name.startswith(("systemd-sleep", "nvidia", "kwin_wayland",
                                                    "cardwired", "DisplayLink")):
                continue
            chunks.append(f"\nPID {proc.name} {name}\n")
            for task in (proc / "task").glob("[0-9]*"):
                try:
                    chunks.append(f"TID {task.name}: {(task / 'comm').read_text().strip()}\n")
                    chunks.append((task / "stack").read_text())
                except OSError as exc:
                    chunks.append(f"[stack unavailable: {exc}]\n")
        except OSError:
            continue
    (folder / f"stacks-{index}.txt").write_text("".join(chunks))
    # 'w' only dumps blocked tasks. No reset, kill, or power action is issued.
    try:
        Path("/proc/sysrq-trigger").write_text("w")
    except OSError as exc:
        (folder / f"sysrq-{index}.txt").write_text(str(exc))
    snapshot(folder, f"delayed-{index}")
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        command(["journalctl", "--sync"], timeout=5)
    os.sync()


def observer(folder, done):
    # These delays measure elapsed execution; freezing pauses this process too.
    for index, delay in enumerate((15, 30), 1):
        if done.wait(delay):
            return
        print(f"Delayed stack capture {index}: transition has not completed.", flush=True)
        dump_stacks(folder, index)


def create_override(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        try:
            stream.write(text)
            stream.flush()
        except BaseException:
            path.unlink()
            raise


def label_overrides(paths):
    # mkdir/open inherits init_var_run_t under /run/systemd. systemd-sleep's
    # SELinux domain cannot read that directory as a sleep configuration.
    # Apply existing policy labels, not a custom allow rule or permissive mode.
    if not Path("/sys/fs/selinux/enforce").exists():
        return
    targets = list(dict.fromkeys(str(p) for path in paths for p in (path.parent, path)))
    require_command(["restorecon", "-F", *targets])
    require_command(["matchpathcon", "-V", *targets])


def finish_settings(saved, owned):
    # Restore only once the requested cycle has finished (or before requesting).
    errors = []
    actions = [(DEPTH, "default"), (PM_TEST, saved["pm_test"])]
    actions.extend((Path(path), value) for path, value in saved["flags"].items())
    for path, value in actions:
        try:
            path.write_text(value + "\n")
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    for path in owned:
        try:
            path.unlink()
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    result = command(["systemctl", "daemon-reload"])
    if result.returncode:
        errors.append(result.stdout)
    if errors:
        raise RuntimeError("Cleanup incomplete: " + "; ".join(errors))


def cleanup_rejected(folder):
    """Explicit recovery only for a request that was refused before enqueue."""
    folder = folder.resolve()
    if os.geteuid() != 0 or folder.parent != LOG_ROOT or folder.stat().st_uid != 0:
        raise RuntimeError("Expected a root-owned run directory under the diagnostic log root.")
    with LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        saved = json.loads((folder / "before.json").read_text())
        if saved["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
            raise RuntimeError("This run belongs to another boot; do not change current settings.")
        request = (folder / "request.txt").read_text()
        if not definitive_rejection(request) or not request.rstrip().endswith("exit=1"):
            raise RuntimeError("This request was not definitively rejected; manual inspection required.")
        if saved.get("pm_test") != "none" or set(saved.get("flags", {})) != {str(p) for p in FLAGS}:
            raise RuntimeError("Unexpected saved settings; refusing recovery.")
        if any(value not in ("0", "1") for value in saved["flags"].values()):
            raise RuntimeError("Unexpected saved PM logging value.")
        expected = {SLEEP_CONF: "[Sleep]\nSuspendState=\nSuspendState=mem\n",
                    UNIT_CONF: unit_override(folder)}
        for path, content in expected.items():
            if path.is_symlink() or path.read_text() != content:
                raise RuntimeError(f"Override does not belong to this run: {path}")
        if is_busy(unit_state("egpu-sleep-lab-" + folder.name + ".service")):
            raise RuntimeError("The diagnostic worker is still active.")
        assert_sleep_idle()
        finish_settings(saved, list(expected))
        (folder / "restored.txt").write_text("Rejected request: verified idle and restored saved settings without reboot.\n")
        print("Rejected request cleaned up. No sleep or reboot requested.", flush=True)


def worker(folder, stage="freezer", guard_bypass=False):
    validate_stage(stage)
    with LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return experiment(folder, stage, guard_bypass)


def experiment(folder, stage="freezer", guard_bypass=False):
    validate_stage(stage)
    info = preflight(stage, guard_bypass)
    saved = {"pm_test": selected(PM_TEST.read_text()),
             "flags": {str(path): path.read_text().strip() for path in FLAGS}}
    (folder / "before.json").write_text(json.dumps({**info, **saved}, indent=2))
    owned = []
    request_may_be_pending = False
    cycle_finished = False
    done = threading.Event()
    thread = None
    started_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        snapshot(folder, "before")
        create_override(SLEEP_CONF, "[Sleep]\nSuspendState=\nSuspendState=mem\n")
        owned.append(SLEEP_CONF)
        create_override(UNIT_CONF, unit_override(folder))
        owned.append(UNIT_CONF)
        label_overrides(owned)
        require_command(["systemctl", "daemon-reload"])
        config = require_command(["systemd-analyze", "cat-config", "systemd/sleep.conf"])
        (folder / "effective-sleep.conf").write_text(config)
        if effective_suspend_states(config) != ["mem"]:
            raise RuntimeError("Could not enforce exactly one sleep state: mem.")
        environment = require_command(["systemctl", "show", "systemd-suspend.service", "-p", "Environment"])
        (folder / "sleep-environment.txt").write_text(environment)
        if "SYSTEMD_LOG_LEVEL=debug" not in environment:
            raise RuntimeError("Sleep-service debug logging was not enabled.")
        for path in FLAGS:
            path.write_text("1\n")
        PM_TEST.write_text(stage + "\n")
        if selected(PM_TEST.read_text()) != stage:
            raise RuntimeError(f"Kernel did not accept the {stage}-only test.")
        DEPTH.write_text("default\n")
        if guard_bypass:
            arm_guard_platform_once(folder)
        baseline = unit_state("systemd-suspend.service")["ExecMainStartTimestampMonotonic"]
        (folder / "request-baseline.txt").write_text(baseline + "\n")
        print(f"{stage.upper()} TEST, NVIDIA depth=default. Logs: {folder}", flush=True)
        if stage == "freezer":
            print("Requesting one suspend through logind; no retry and no device/platform sleep phase.", flush=True)
        elif stage == "devices":
            print("One device suspend/resume test; no retry, no late/noirq/platform sleep phase. "
                  "Userspace stack capture may freeze with other tasks.", flush=True)
        else:
            print("One late/noirq/platform callback test; no retry and no real s2idle wait loop. "
                  "Userspace stack capture may freeze with other tasks.", flush=True)
        require_command(["journalctl", "--sync"])
        os.sync()
        thread = threading.Thread(target=observer, args=(folder, done), daemon=True)
        thread.start()
        # Mark pending BEFORE requesting: a timeout does not cancel a queued sleep.
        request_may_be_pending = True
        request = command(SUSPEND_REQUEST, timeout=30)
        (folder / "request.txt").write_text(request.stdout + f"\nexit={request.returncode}\n")
        if request.stdout.strip():
            print("Sleep request response: " + request.stdout.strip(), flush=True)
        if request.returncode:
            if definitive_rejection(request.stdout):
                assert_sleep_idle(baseline)
                request_may_be_pending = False
                raise RuntimeError("Sleep request was refused before the test; restoring temporary settings.")
            # Transport failures/timeouts do not prove that no queued job exists.
            raise RuntimeError("Sleep request returned an error; retaining temporary test settings pending inspection.")
        state = wait_for_cycle(baseline, completion=folder / "sleep-unit-ended.json")
        cycle_finished = True
        (folder / "cycle.json").write_text(json.dumps(state, indent=2))
        done.set()
        if thread:
            thread.join(timeout=20)
        snapshot(folder, "after")
        log = require_command(["journalctl", "-k", "-b", "--since", started_at,
                               "--no-pager", "-o", "short-monotonic"])
        (folder / "kernel-test.txt").write_text(log)
        # The journal reception timestamp shifts when journald is frozen.
        # Preserve _SOURCE_MONOTONIC_TIMESTAMP for actual kernel event timing.
        save_command(folder / "kernel-events.txt", ["journalctl", "-k", "-b", "--since", started_at,
                     "--no-pager", "-o", "json"])
        passed = stage_confirmed(stage, state, log)
        (folder / "result.txt").write_text(
            (f"{stage.upper()} STAGE RETURNED; inspect driver errors before any deeper test.\n" if passed else
             f"{stage.upper()} STAGE NOT CONFIRMED; inspect logs. Do not repeat or deepen automatically.\n"))
        # No NVML/ioctl after a possibly broken GPU transition: restore settings
        # first and let the operator decide whether to query NVIDIA afterwards.
        print((folder / "result.txt").read_text().strip(), flush=True)
        return 0 if passed else 1
    finally:
        done.set()
        if guard_bypass:
            # If ExecStartPre did not consume it, fail closed for every later request.
            SLEEP_GUARD_ONCE.unlink(missing_ok=True)
        cleanup_error = None
        if not request_may_be_pending or cycle_finished:
            try:
                finish_settings(saved, owned)
                (folder / "restored.txt").write_text("pm_test, PM logging and runtime overrides restored; NVIDIA depth=default.\n")
                print("Temporary settings restored.", flush=True)
            except Exception as exc:
                cleanup_error = exc
                (folder / "cleanup-error.txt").write_text(str(exc) + "\nDo not request sleep again until inspected.\n")
        else:
            (folder / "reboot-clears-test.txt").write_text(
                f"The sleep request may still be in flight. Keep pm_test={stage} and depth=default.\n"
                "Do not request another sleep. A reboot clears these /run and driver/sysfs settings.\n"
                "No automatic reboot or GPU reset was attempted.\n")
            print("Transition outcome uncertain: test settings retained until reboot; do not request sleep again.", flush=True)
        save_command(folder / "services.txt", ["journalctl", "-b", "--since", started_at,
                     "-u", "systemd-suspend.service", "-u", "prepare-kio-for-sleep.service",
                     "-u", "disarm-usb-wake-for-sleep.service", "--no-pager", "-o", "short-monotonic"])
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            command(["journalctl", "--sync"], timeout=5)
        os.sync()
        if cleanup_error:
            raise cleanup_error


def launch(stage="freezer", guard_bypass=False):
    validate_stage(stage)
    info = preflight(stage, guard_bypass)
    LOG_ROOT.mkdir(mode=0o700, exist_ok=True)
    if LOG_ROOT.is_symlink() or LOG_ROOT.stat().st_uid != 0 or LOG_ROOT.stat().st_mode & 0o022:
        raise RuntimeError("Log root must be a root-owned directory, not writable by other users.")
    import tempfile
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = Path(tempfile.mkdtemp(prefix=stamp + "-", dir=LOG_ROOT))
    worker_file = folder / "worker.py"
    shutil.copyfile(Path(__file__).resolve(), worker_file)
    worker_file.chmod(0o600)
    (folder / "preflight.json").write_text(json.dumps(info, indent=2))
    unit = "egpu-sleep-lab-" + folder.name
    (folder / "unit.txt").write_text(unit + ".service\n")
    print(f"Stage: {stage}\nLogs: {folder}\nTransient service: {unit}.service", flush=True)
    worker_argv = ["/usr/bin/python3", str(worker_file), "_worker", "--stage", stage]
    if guard_bypass:
        worker_argv.append("--host-reset-guard-bypass")
    result = command(["systemd-run", "--collect", "--unit=" + unit,
                      "--service-type=exec", "--property=TimeoutStopSec=15s",
                      "--property=Restart=no", *worker_argv], timeout=15)
    print(result.stdout, end="", flush=True)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "run", "cleanup", "_cleanup", "_worker", "_complete"))
    parser.add_argument("run_directory", nargs="?")
    parser.add_argument("--stage", choices=TEST_STAGES,
                        help="Explicit PM test stage; defaults to freezer. Never enables full sleep.")
    parser.add_argument("--host-reset-guard-bypass", action="store_true",
                        help="Consume one sleep-guard exception for the explicit no-dock host_reset=0 platform test.")
    args = parser.parse_args()
    if args.stage and args.action not in ("check", "run", "_worker"):
        parser.error("--stage only applies to check/run")
    stage = args.stage or "freezer"
    if args.host_reset_guard_bypass and (args.action not in ("check", "run", "_worker") or stage != "platform"):
        parser.error("--host-reset-guard-bypass requires check/run --stage platform")
    if args.action in ("cleanup", "_cleanup"):
        if not args.run_directory or os.geteuid() != 0:
            parser.error("cleanup needs root and an explicit diagnostic run directory")
        if args.action == "_cleanup":
            cleanup_rejected(Path(args.run_directory))
            return 0
        # Block ordinary new sleep requests while verifying and restoring.
        result = command(["systemd-inhibit", "--what=sleep", "--mode=block", "--who=egpu-sleep-lab",
                          "--why=Restoring a rejected diagnostic test", "/usr/bin/python3",
                          str(Path(__file__).resolve()), "_cleanup", args.run_directory], timeout=30)
        print(result.stdout, end="", flush=True)
        return result.returncode
    if args.run_directory:
        parser.error("a run directory is only accepted for cleanup")
    if args.action == "check":
        print(json.dumps(preflight(stage, args.host_reset_guard_bypass), indent=2))
        print("READ-ONLY PREFLIGHT PASSED. No sleep requested and no configuration changed.")
        return 0
    if args.action == "run":
        return launch(stage, args.host_reset_guard_bypass)
    folder = Path(__file__).resolve().parent
    if os.geteuid() != 0 or folder.parent != LOG_ROOT or folder.stat().st_uid != 0:
        raise RuntimeError("Worker must run from its root-owned diagnostic log directory.")
    if args.action == "_complete":
        # Atomic publication: the polling worker must never see partial JSON.
        marker = folder / "sleep-unit-ended.json.tmp"
        marker.write_text(json.dumps({"service_result": os.environ.get("SERVICE_RESULT", "unknown"),
                                      "exit_code": os.environ.get("EXIT_CODE", ""),
                                      "exit_status": os.environ.get("EXIT_STATUS", "")}))
        marker.replace(folder / "sleep-unit-ended.json")
        return 0
    def interrupted(*_):
        raise InterruptedError("Worker stopped")
    signal.signal(signal.SIGTERM, interrupted)
    return worker(folder, stage, args.host_reset_guard_bypass)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
