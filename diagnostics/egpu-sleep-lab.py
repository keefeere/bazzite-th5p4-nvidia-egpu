#!/usr/bin/python3
"""One explicit eGPU PM test, or --graphics-only logout/restart without sleep.

Run on the host as root. No permanent unit, module option or kernel argument is
installed. A copied worker in a transient system service owns the experiment.
"""

import argparse
import contextlib
import datetime as dt
import fcntl
import importlib.util
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
PM_ASYNC = Path('/sys/power/pm_async')
PM_TRACE = Path('/sys/power/pm_trace')
SLEEP_GUARD = Path("/etc/egpu-nvidia/egpu-sleep-guard.sh")
SLEEP_GUARD_ONCE = Path("/run/egpu-sleep-guard-platform-once")
HOST_RESET = Path("/sys/module/thunderbolt/parameters/host_reset")
CMDLINE = Path("/proc/cmdline")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
CARDWIRE_MASK = Path("/run/systemd/system/cardwired.service")
PROC_ROOT = Path("/proc")
NO_GRAPHICS_CHECK = Path("/etc/egpu-nvidia/egpu-no-graphics-check.py")
TRANSITION_LOCK = Path("/run/egpu-nvidia-transition.lock")
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
SERIAL_MODE = 'vram-tmpfs-serial'
RTC_MODE = 'vram-tmpfs-serial-rtc'
SERIAL_MODES = (SERIAL_MODE, RTC_MODE)
PRIVATE_VRAM_MODES = ('vram-tmpfs', *SERIAL_MODES)
VRAM_MODES = ('vram-current', *PRIVATE_VRAM_MODES)
VRAM_CHECK = Path('/etc/egpu-nvidia/egpu-vram-backing-check.py')
PROCFS_PM_ACTIVE = Path('/run/egpu-nvidia-procfs-pm-boot')
PROCFS_PM_HOOK = Path('/etc/egpu-nvidia/egpu-nvidia-procfs-pm.py')
PROCFS_PM_MARKER_OWNER = 0


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


def nvidia_pm_mode(params, boot_id):
    """Accept the normal notifier path or exactly our paired one-boot procfs path."""
    notifier = re.findall(r'^UseKernelSuspendNotifiers:\s*([01])\s*$', params, re.M)
    if len(notifier) != 1:
        raise RuntimeError('NVIDIA PM notifier mode is missing or ambiguous.')
    active = PROCFS_PM_ACTIVE.exists() or PROCFS_PM_ACTIVE.is_symlink()
    if notifier[0] == '1':
        if active:
            raise RuntimeError('Procfs PM boot marker exists but NVIDIA still uses kernel notifiers.')
        return 'kernel-notifier'
    if not active or PROCFS_PM_ACTIVE.is_symlink() or not PROCFS_PM_ACTIVE.is_file():
        raise RuntimeError('NVIDIA notifier is disabled without a valid procfs PM boot marker.')
    stat = PROCFS_PM_ACTIVE.stat()
    if (stat.st_uid != PROCFS_PM_MARKER_OWNER or stat.st_mode & 0o777 != 0o600
            or PROCFS_PM_ACTIVE.read_text().strip() != boot_id):
        raise RuntimeError('NVIDIA procfs PM boot marker is stale or unsafe.')
    if not PROCFS_PM_HOOK.is_file():
        raise RuntimeError('Paired NVIDIA procfs PM hook is not installed.')
    pre = require_command(['systemctl', 'show', 'systemd-suspend.service', '-p', 'ExecStartPre', '--value'])
    post = require_command(['systemctl', 'show', 'systemd-suspend.service', '-p', 'ExecStopPost', '--value'])
    guard_entry = f'argv[]=/usr/bin/bash {SLEEP_GUARD} systemd-suspend.service ; ignore_errors=no'
    pre_entry = f'argv[]=/usr/bin/python3 {PROCFS_PM_HOOK} pre ; ignore_errors=no'
    post_entry = f'argv[]=/usr/bin/python3 {PROCFS_PM_HOOK} post ; ignore_errors=no'
    guard_at = pre.find(guard_entry)
    procfs_at = pre.find(pre_entry)
    if guard_at < 0 or procfs_at <= guard_at or post_entry not in post:
        raise RuntimeError('NVIDIA procfs PM hook is missing, ignored or ordered before the sleep guard.')
    return 'paired-procfs'


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


def compute_client_summary(clients):
    # Recent NVML output may contain a whole command line. This is display-only:
    # do not treat slashes in arguments (e.g. --render-node=/dev/dri/...) as the
    # executable or disclose the application's full arguments in the error.
    labels = []
    for pid, name in clients[:8]:
        token = name.split(maxsplit=1)[0] if name.strip() else 'unknown'
        label = re.sub(r'[^\w.+-]', '?', Path(token).name)[:64] or 'unknown'
        labels.append(f'{pid}:{label}')
    if len(clients) > 8:
        labels.append(f'+{len(clients) - 8} more')
    return ', '.join(labels)


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


def unit_override(folder, experiment=None):
    override = ("[Service]\nEnvironment=SYSTEMD_LOG_LEVEL=debug\n"
                f"ExecStopPost=/usr/bin/python3 {folder / 'worker.py'} _complete\n")
    if experiment == "no-graphics":
        # The packaged DisplayLink hook writes to FIFOs even with its daemon
        # stopped. Replace only this unit's view, never the host's /usr file.
        override += "BindReadOnlyPaths=/usr/bin/true:/usr/lib/systemd/system-sleep/displaylink\n"
    if experiment in PRIVATE_VRAM_MODES:
        override += 'TemporaryFileSystem=' + vram_module().TMPFS + '\n'
    return override


def vram_module():
    path = Path(__file__).resolve().parent / 'egpu_vram_backing.py'
    spec = importlib.util.spec_from_file_location('egpu_vram_backing', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def no_graphics_module():
    path = Path(__file__).resolve().parent / 'egpu_no_graphics.py'
    spec = importlib.util.spec_from_file_location('egpu_no_graphics', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def trace_module():
    path = Path(__file__).resolve().parent / 'egpu_pm_trace.py'
    spec = importlib.util.spec_from_file_location('egpu_pm_trace', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def validate_cardwire_off():
    if not CARDWIRE_MASK.is_symlink() or os.readlink(CARDWIRE_MASK) != "/dev/null":
        raise RuntimeError("Cardwire must be runtime-masked for this diagnostic.")
    if unit_state("cardwired.service").get("ActiveState") != "inactive":
        raise RuntimeError("Cardwire must be inactive for this diagnostic.")
    for task in PROC_ROOT.glob("[0-9]*/comm"):
        try:
            if task.read_text().strip() == "cardwired":
                raise RuntimeError("A cardwired process is still running.")
        except FileNotFoundError:
            # A process may exit while the read-only snapshot is taken.
            continue


def validate_guard_bypass(stage, experiment="host-reset", *, preparing=False):
    if experiment in VRAM_MODES:
        helper = vram_module()
        if stage not in helper.STAGES:
            raise RuntimeError('Backing-store diagnostics allow only freezer/devices/platform; no full sleep.')
        if experiment in SERIAL_MODES:
            if stage != 'platform':
                raise RuntimeError('Serial device-PM comparison requires an explicit platform test.')
            helper.check_device_policy(experiment, preparing=True)
        helper.check_host()
        helper.check_service_policy(sys.modules[__name__])
        helper.check_current_mount()
        if helper.MARKER.exists() or helper.MARKER.is_symlink():
            raise RuntimeError('A stale backing-store exception exists; inspect before testing.')
        if VRAM_CHECK.is_symlink() or not VRAM_CHECK.is_file():
            raise RuntimeError('Install the backing-store checker before this diagnostic.')
        status = VRAM_CHECK.stat()
        if (status.st_uid != 0 or status.st_mode & 0o022
                or VRAM_CHECK.read_bytes() != Path(helper.__file__).read_bytes()):
            raise RuntimeError('Installed backing-store checker is unsafe or differs; reinstall the guard.')
    elif stage != "platform":
        raise RuntimeError("The sleep-guard bypass is valid only for the explicit platform test.")
    if experiment == "host-reset":
        for name, value in (("thunderbolt.host_reset", "0"),
                            ("egpu.host_reset_test", "1"),
                            ("egpu.host_reset_nodock", "1")):
            exact_cmdline_argument(name, value)
        if HOST_RESET.read_text().strip() != "N":
            raise RuntimeError("The live Thunderbolt host_reset value is not N.")
    elif experiment in ("cardwire-off", "no-graphics"):
        if HOST_RESET.read_text().strip() != "Y" or any(
                token.split("=", 1)[0] in ("thunderbolt.host_reset", "egpu.host_reset_test", "egpu.host_reset_nodock")
                for token in CMDLINE.read_text().split()):
            raise RuntimeError("This diagnostic requires the normal host_reset=Y boot, without A/B arguments.")
        if experiment == "cardwire-off":
            validate_cardwire_off()
        else:
            if NO_GRAPHICS_CHECK.is_symlink() or not NO_GRAPHICS_CHECK.is_file():
                raise RuntimeError("Install the no-graphics checker before this diagnostic.")
            status = NO_GRAPHICS_CHECK.stat()
            if status.st_uid != 0 or status.st_mode & 0o022:
                raise RuntimeError("The installed no-graphics checker must be root-owned and not writable by others.")
            if not preparing:
                require_command(['/usr/bin/python3', str(NO_GRAPHICS_CHECK)])
    elif experiment not in VRAM_MODES:
        raise RuntimeError("Unknown sleep-guard diagnostic.")
    if not SLEEP_GUARD.is_file() or SLEEP_GUARD.is_symlink():
        raise RuntimeError("The installed eGPU sleep guard is missing or is a symlink.")
    if experiment == "cardwire-off" and "EGPU_SLEEP_GUARD_CARDWIRE_OFF_V1" not in SLEEP_GUARD.read_text():
        raise RuntimeError("Install the Cardwire-aware sleep guard before this diagnostic.")
    if experiment == "no-graphics" and "EGPU_SLEEP_GUARD_NO_GRAPHICS_V1" not in SLEEP_GUARD.read_text():
        raise RuntimeError("Install the no-graphics sleep guard before this diagnostic.")
    if experiment in VRAM_MODES and 'EGPU_SLEEP_GUARD_VRAM_STAGES_V4' not in SLEEP_GUARD.read_text():
        raise RuntimeError('Install the backing-store-aware sleep guard before this diagnostic.')
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


def arm_guard_platform_once(folder, experiment="host-reset"):
    validate_guard_bypass("platform", experiment)
    if selected(PM_TEST.read_text()) != "platform":
        raise RuntimeError("Refusing to arm an exception without pm_test=platform.")
    boot_id = BOOT_ID.read_text().strip()
    payload = (f"{boot_id}\nplatform\n" + (experiment + "\n" if experiment != "host-reset" else "")).encode()
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
            f"Armed one {experiment} platform-only invocation for this boot; "
            "the guard consumes it before systemd-sleep.\n")
    except BaseException:
        SLEEP_GUARD_ONCE.unlink(missing_ok=True)
        raise


def preflight(stage="freezer", guard_bypass=False, *, preparing=False):
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
    pm_transport = nvidia_pm_mode(params, BOOT_ID.read_text().strip())
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
        raise RuntimeError("NVIDIA reports compute/UVM clients besides KWin: "
                           f"{compute_client_summary(clients)}. This diagnostic requires them closed; "
                           "desktop/browser apps may appear here even without a heavy workload. "
                           "Save work and exit the listed apps normally; no sleep requested.")
    user_session_freezer_policy = None
    if guard_bypass == "no-graphics":
        validate_guard_bypass(stage, guard_bypass, preparing=preparing)
    elif guard_bypass == "cardwire-off" or guard_bypass in VRAM_MODES:
        validate_guard_bypass(stage, guard_bypass)
        if guard_bypass in VRAM_MODES:
            user_session_freezer_policy = vram_module().check_service_policy(sys.modules[__name__])
    elif guard_bypass:
        validate_guard_bypass(stage)
    info = {"kernel": os.uname().release,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "gpu": gpu.strip(), "pm_test": "none", "requested_stage": stage,
            "nvidia_depth_for_test": "default",
            "nvidia_pm_transport": pm_transport,
            "sleep_guard_bypass": f"one {stage} invocation" if guard_bypass else "disabled",
            "experiment": guard_bypass if guard_bypass in ("cardwire-off", "no-graphics", *VRAM_MODES) else
                          ("host-reset" if guard_bypass else "unmodified"),
            "warning": f"{stage} testing invokes the NVIDIA {pm_transport} PM path and may hang the GPU or host."
                       + (" Devices will be suspended/resumed; stack capture may be frozen too."
                          if stage in ("devices", "platform") else "")
                       + (" Late/noirq and platform callbacks will run, but not the s2idle wait loop."
                          if stage == "platform" else "")}
    if guard_bypass == 'no-graphics' and preparing:
        info['logout_plan'] = no_graphics_module().plan(sys.modules[__name__])
        info['warning'] += ' Running this test ends the configured graphical session; save all work.'
    if guard_bypass in VRAM_MODES:
        info['backing_store'] = ('private 6 GiB tmpfs, huge=never, noswap' if guard_bypass in PRIVATE_VRAM_MODES
                                 else 'unchanged host /var/tmp (Btrfs)')
        info['user_session_freezer_policy'] = user_session_freezer_policy
        info['warning'] += ' Session stays running, but a hang may still require forced reboot. No automatic retry.'
    if guard_bypass in SERIAL_MODES:
        info['device_pm'] = 'one serial device-callback test (pm_async: 1 -> 0 -> restore)'
    if guard_bypass == RTC_MODE:
        config = Path('/usr/lib/modules') / os.uname().release / 'config'
        if not config.is_file() or 'CONFIG_PM_TRACE_RTC=y' not in config.read_text().splitlines():
            raise RuntimeError('RTC PM tracing is not supported by this exact kernel.')
        if PM_TRACE.read_text().strip() != '0':
            raise RuntimeError('RTC PM tracing must be off before this diagnostic.')
        info['rtc_fingerprint'] = ('One platform-only PM fingerprint in RTC memory. This overwrites RTC clock data; '
                                   'wall-clock time may be wrong until NTP resynchronizes. It is not a sleep fix.')
        info['warning'] += ' RTC contents will change; forced reboot may be needed after a hang.'
    return info


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
                       ("pm-async", "/sys/power/pm_async"),
                       ("pm-trace", "/sys/power/pm_trace"),
                       ("cmdline", "/proc/cmdline"),
                       ("host-reset", "/sys/module/thunderbolt/parameters/host_reset"),
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
    save_command(dest / "cardwire-unit.txt", ["systemctl", "show", "cardwired.service",
                                              "-p", "ActiveState", "-p", "SubState",
                                              "-p", "UnitFileState", "-p", "MainPID"])
    save_command(dest / "kernel.txt", ["journalctl", "-k", "-b", "--no-pager", "-n", "180",
                                        "-o", "short-monotonic"])


def stack_candidates(focused=False):
    """Put the PM caller first, before potentially numerous NVIDIA threads."""
    candidates = []
    for proc in PROC_ROOT.glob("[0-9]*"):
        try:
            name = (proc / "comm").read_text().strip()
            if name == "systemd-sleep":
                priority = 0
            elif focused:
                continue
            elif name.startswith(("nvidia", "kwin_wayland", "cardwired", "DisplayLink")):
                priority = 1
            elif re.search(r"^State:\s+D\b", (proc / "status").read_text(), re.M):
                priority = 2
            else:
                continue
            candidates.append((priority, int(proc.name), proc, name))
        except OSError:
            continue
    return [(proc, name) for _, _, proc, name in sorted(candidates)]


def capture_task_context(task, persist):
    # ORC can refuse to unwind a task running on another CPU. Save independent
    # scheduler observations before trying the stack, without stopping the task
    # or issuing a GPU ioctl. These reads are NOT an atomic task snapshot.
    status_fields = {'Name', 'State', 'Tgid', 'Pid', 'PPid', 'TracerPid',
                     'voluntary_ctxt_switches', 'nonvoluntary_ctxt_switches'}
    for name in ('status', 'wchan', 'schedstat'):
        stamp = time.monotonic()
        try:
            value = (task / name).read_text().strip()
            if name == 'status':
                value = '\n'.join(line for line in value.splitlines()
                                  if line.partition(':')[0] in status_fields)
            persist(f'{name}: sample_monotonic={stamp:.6f}\n'
                    + (value or '[empty observation; not evidence of completion]') + '\n')
        except OSError as exc:
            persist(f'[{name} unavailable at {stamp:.6f}: {exc}]\n')


def capture_stacks(folder, index, focused=False):
    # No GPU ioctls/NVML or PM writes. Flush each task before reading the next;
    # a stalled later read must not discard already collected PM evidence.
    path = folder / f"stacks-{index}.txt"
    with path.open("x") as stream:
        def persist(text):
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())

        persist(f"monotonic={time.monotonic():.6f} focused={focused}\n"
                "Userspace sampling only; no capture is guaranteed after freezing.\n"
                "Task context reads are non-atomic; wchan=0 or an empty stack is inconclusive.\n")
        candidates = stack_candidates(focused)
        if not candidates:
            persist("[no matching process observed; PM stage cannot be inferred]\n")
        for proc, name in candidates:
            persist(f"\nPID {proc.name} {name}\n")
            if name == 'systemd-sleep':
                try:
                    persist('mount_namespace=' + os.readlink(proc / 'ns/mnt') + '\n')
                    for line in (proc / 'mountinfo').read_text().splitlines():
                        fields = line.split()
                        if len(fields) > 4 and fields[4] == '/var/tmp':
                            persist('backing_mount=' + line + '\n')
                except OSError as exc:
                    persist(f'[mount evidence unavailable: {exc}]\n')
            for task in sorted((proc / "task").glob("[0-9]*"), key=lambda p: int(p.name)):
                persist(f"TID {task.name}: sample_monotonic={time.monotonic():.6f}\n")
                capture_task_context(task, persist)
                persist(f"stack_sample_monotonic={time.monotonic():.6f}\n")
                try:
                    stack = (task / "stack").read_text()
                    persist(stack if stack else "[empty kernel stack; not a completion signal]\n")
                except OSError as exc:
                    persist(f"[stack unavailable: {exc}]\n")


def dump_stacks(folder, index, focused=False):
    capture_stacks(folder, index, focused)
    if focused:
        # Keep early probes small: no all-thread sweep, sysrq, global sync,
        # systemctl, journal query or GPU access on the pre-freezer path.
        return
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
    # Deadlines are relative to observer start; captures themselves take time.
    # This process freezes too and is not a watchdog. Check done before capture
    # even if a deadline elapsed while this process was unable to run.
    started = time.monotonic()
    for index, offset, focused in (("early-1", 2, True), ("early-2", 6, True),
                                   (1, 15, False), (2, 45, False)):
        if done.wait(max(0, started + offset - time.monotonic())):
            return
        print(f"Delayed stack capture {index}: transition has not completed.", flush=True)
        try:
            dump_stacks(folder, index, focused=focused)
        except OSError as exc:
            print(f"Stack capture {index} unavailable: {exc}", flush=True)


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
    actions = [(DEPTH, "default")]
    if 'pm_async' in saved:
        if saved['pm_async'] not in ('0', '1'):
            raise RuntimeError('Unexpected saved pm_async value; refusing restoration.')
        actions.append((PM_ASYNC, saved['pm_async']))
    if 'pm_trace' in saved:
        if saved['pm_trace'] != '0':
            raise RuntimeError('Unexpected saved pm_trace value; refusing restoration.')
        actions.append((PM_TRACE, saved['pm_trace']))
    actions.append((PM_TEST, saved['pm_test']))
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
                    UNIT_CONF: unit_override(folder, saved.get('experiment'))}
        for path, content in expected.items():
            if path.is_symlink() or path.read_text() != content:
                raise RuntimeError(f"Override does not belong to this run: {path}")
        if is_busy(unit_state("egpu-sleep-lab-" + folder.name + ".service")):
            raise RuntimeError("The diagnostic worker is still active.")
        assert_sleep_idle()
        finish_settings(saved, list(expected))
        (folder / "restored.txt").write_text("Rejected request: verified idle and restored saved settings without reboot.\n")
        print("Rejected request cleaned up. No sleep or reboot requested.", flush=True)


def worker(folder, stage="freezer", guard_bypass=False, *, kernel_trace=False):
    validate_stage(stage)
    with LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if guard_bypass in ('no-graphics', 'graphics-only'):
            if kernel_trace:
                raise RuntimeError('Kernel tracing is restricted to session-intact PM tests.')
            if guard_bypass == 'no-graphics' and stage != 'platform':
                raise RuntimeError('The no-graphics test is platform-only.')
            with TRANSITION_LOCK.open('a') as transition:
                fcntl.flock(transition, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return no_graphics_module().run(sys.modules[__name__], folder,
                                               graphics_only=guard_bypass == 'graphics-only')
        return experiment(folder, stage, guard_bypass, kernel_trace=kernel_trace)


def experiment(folder, stage="freezer", guard_bypass=False, *, kernel_trace=False):
    validate_stage(stage)
    info = preflight(stage, guard_bypass)
    saved = {"pm_test": selected(PM_TEST.read_text()),
             "flags": {str(path): path.read_text().strip() for path in FLAGS}}
    if guard_bypass in SERIAL_MODES:
        saved['pm_async'] = PM_ASYNC.read_text().strip()
        if saved['pm_async'] != '1':
            raise RuntimeError('Serial comparison expects an unchanged pm_async=1 baseline.')
    if guard_bypass == RTC_MODE:
        saved['pm_trace'] = PM_TRACE.read_text().strip()
        if saved['pm_trace'] != '0':
            raise RuntimeError('RTC PM tracing must start disabled.')
    (folder / "before.json").write_text(json.dumps({**info, **saved}, indent=2))
    owned = []
    request_may_be_pending = False
    cycle_finished = False
    done = threading.Event()
    thread = None
    capture = None
    started_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        snapshot(folder, "before")
        if kernel_trace:
            capture = trace_module().Capture(folder)
            # Refuse missing targets/failed marker before changing PM policy.
            capture.start()
        create_override(SLEEP_CONF, "[Sleep]\nSuspendState=\nSuspendState=mem\n")
        owned.append(SLEEP_CONF)
        create_override(UNIT_CONF, unit_override(folder, guard_bypass))
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
        if guard_bypass in SERIAL_MODES:
            PM_ASYNC.write_text('0\n')
            if PM_ASYNC.read_text().strip() != '0':
                raise RuntimeError('Kernel did not accept serial device PM; no sleep requested.')
        if guard_bypass == RTC_MODE:
            PM_TRACE.write_text('1\n')
            if PM_TRACE.read_text().strip() != '1':
                raise RuntimeError('Kernel did not accept RTC PM tracing; no sleep requested.')
        if guard_bypass in VRAM_MODES:
            vram_module().arm(folder, guard_bypass, stage=stage)
        elif guard_bypass in ("cardwire-off", "no-graphics"):
            arm_guard_platform_once(folder, guard_bypass)
        elif guard_bypass:
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
        if capture:
            capture.ensure_running()
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
        if guard_bypass in VRAM_MODES:
            vram_module().MARKER.unlink(missing_ok=True)
        elif guard_bypass:
            # If ExecStartPre did not consume it, fail closed for every later request.
            SLEEP_GUARD_ONCE.unlink(missing_ok=True)
        if capture:
            try:
                capture.stop()
            except Exception as exc:
                # Preserve the original PM outcome and always reach PM cleanup.
                print(f'Trace cleanup failed: {exc}; inspect the private instance.', flush=True)
        cleanup_error = None
        if not request_may_be_pending or cycle_finished:
            try:
                finish_settings(saved, owned)
                (folder / "restored.txt").write_text("pm_test, PM logging and runtime overrides restored; NVIDIA depth=default.\n"
                    + ("pm_async restored to its saved value.\n" if 'pm_async' in saved else "")
                    + ("pm_trace disabled; RTC wall-clock data may still need NTP resync.\n"
                       if 'pm_trace' in saved else ""))
                print("Temporary settings restored.", flush=True)
            except Exception as exc:
                cleanup_error = exc
                (folder / "cleanup-error.txt").write_text(str(exc) + "\nDo not request sleep again until inspected.\n")
        else:
            (folder / "reboot-clears-test.txt").write_text(
                f"The sleep request may still be in flight. Keep pm_test={stage} and depth=default.\n"
                "Do not request another sleep. A reboot clears these /run and driver/sysfs settings.\n"
                + ("pm_async=0 is retained until idle-state inspection or reboot.\n"
                   if 'pm_async' in saved else "")
                + ("pm_trace=1 was enabled; RTC clock may be wrong after reset.\n"
                   if 'pm_trace' in saved else "RTC tracing was not enabled.\n") +
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


def launch(stage="freezer", guard_bypass=False, *, kernel_trace=False):
    validate_stage(stage)
    if guard_bypass == 'graphics-only':
        info = no_graphics_module().preflight_graphics(sys.modules[__name__])
    else:
        info = (preflight(stage, guard_bypass, preparing=True) if guard_bypass == 'no-graphics'
                else preflight(stage, guard_bypass))
    if kernel_trace:
        if guard_bypass in ('no-graphics', 'graphics-only'):
            raise RuntimeError('Kernel tracing is restricted to session-intact PM tests.')
        info['kernel_trace'] = trace_module().check()
    LOG_ROOT.mkdir(mode=0o700, exist_ok=True)
    if LOG_ROOT.is_symlink() or LOG_ROOT.stat().st_uid != 0 or LOG_ROOT.stat().st_mode & 0o022:
        raise RuntimeError("Log root must be a root-owned directory, not writable by other users.")
    import tempfile
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = Path(tempfile.mkdtemp(prefix=stamp + "-", dir=LOG_ROOT))
    worker_file = folder / "worker.py"
    shutil.copyfile(Path(__file__).resolve(), worker_file)
    worker_file.chmod(0o600)
    if kernel_trace:
        helper = folder / 'egpu_pm_trace.py'
        shutil.copyfile(Path(__file__).resolve().parent / helper.name, helper)
        helper.chmod(0o600)
    if guard_bypass in ('no-graphics', 'graphics-only'):
        helper = folder / 'egpu_no_graphics.py'
        shutil.copyfile(Path(__file__).resolve().parent / helper.name, helper)
        helper.chmod(0o600)
    if guard_bypass in VRAM_MODES:
        helper = folder / 'egpu_vram_backing.py'
        shutil.copyfile(Path(__file__).resolve().parent / helper.name, helper)
        helper.chmod(0o600)
    (folder / "preflight.json").write_text(json.dumps(info, indent=2))
    unit = "egpu-sleep-lab-" + folder.name
    (folder / "unit.txt").write_text(unit + ".service\n")
    label = 'graphics-only (NO SLEEP)' if guard_bypass == 'graphics-only' else stage
    print(f"Stage: {label}\nLogs: {folder}\nTransient service: {unit}.service", flush=True)
    worker_argv = ["/usr/bin/python3", str(worker_file), "_worker"]
    if kernel_trace:
        worker_argv.append('--kernel-trace')
    if guard_bypass == 'graphics-only':
        worker_argv.extend(['--graphics-only', '--allow-logout'])
        # Hold a block inhibitor for the entire roundtrip; the eGPU guard stays
        # installed and no one-shot exception is created.
        worker_argv = ['systemd-inhibit', '--what=sleep', '--mode=block',
                       '--who=egpu-graphics-test', '--why=Graphics-only diagnostic; no sleep',
                       *worker_argv]
    else:
        worker_argv.extend(['--stage', stage])
    if guard_bypass == 'no-graphics':
        worker_argv.extend(['--no-graphics', '--allow-logout'])
    elif guard_bypass == "cardwire-off":
        worker_argv.append("--cardwire-off-guard-bypass")
    elif guard_bypass in VRAM_MODES:
        worker_argv.extend(['--vram-backing', 'private-tmpfs' if guard_bypass in PRIVATE_VRAM_MODES else 'current'])
        if guard_bypass in SERIAL_MODES:
            worker_argv.append('--serial-device-pm')
        if guard_bypass == RTC_MODE:
            worker_argv.append('--rtc-pm-trace')
    elif guard_bypass and guard_bypass != 'graphics-only':
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
    bypasses = parser.add_mutually_exclusive_group()
    bypasses.add_argument("--host-reset-guard-bypass", action="store_true",
                        help="Consume one sleep-guard exception for the explicit no-dock host_reset=0 platform test.")
    bypasses.add_argument("--cardwire-off-guard-bypass", action="store_true",
                         help="One platform-only test on a normal boot with Cardwire already stopped and runtime-masked.")
    bypasses.add_argument('--no-graphics', action='store_true',
                         help='Stop the configured graphical session, SDDM, Cardwire and DisplayLink for one platform test.')
    bypasses.add_argument('--graphics-only', action='store_true',
                         help='Stop and restore the same graphics services without requesting sleep or changing PM settings.')
    bypasses.add_argument('--vram-backing', choices=('current', 'private-tmpfs'),
                         help='One explicit freezer/devices/platform comparison with the session intact; bounded low-VRAM diagnostic, not full sleep.')
    parser.add_argument('--serial-device-pm', action='store_true',
                        help='One explicit platform/private-tmpfs comparison with pm_async=0; RTC tracing remains off unless separately requested.')
    parser.add_argument('--rtc-pm-trace', action='store_true',
                        help='Opt-in RTC fingerprint for a platform/private-tmpfs/serial test; overwrites RTC clock data and may require NTP resync.')
    parser.add_argument('--kernel-trace', action='store_true',
                        help='Optional isolated, bounded PM tracing; does not change the selected test or bypass the guard.')
    parser.add_argument('--allow-logout', action='store_true',
                        help='Required for run --no-graphics or --graphics-only; closes graphical applications.')
    args = parser.parse_args()
    if args.kernel_trace and (args.action not in ('check', 'run', '_worker')
                              or args.no_graphics or args.graphics_only):
        parser.error('--kernel-trace supports only session-intact PM check/run')
    if args.stage and args.action not in ("check", "run", "_worker"):
        parser.error("--stage only applies to check/run")
    stage = args.stage or "freezer"
    if args.serial_device_pm and (args.action not in ('check', 'run', '_worker')
                                  or args.stage != 'platform' or args.vram_backing != 'private-tmpfs'):
        parser.error('--serial-device-pm requires check/run --stage platform --vram-backing private-tmpfs')
    if args.rtc_pm_trace and (not args.serial_device_pm or args.action not in ('check', 'run', '_worker')
                              or args.stage != 'platform' or args.vram_backing != 'private-tmpfs'
                              or args.kernel_trace):
        parser.error('--rtc-pm-trace requires check/run --stage platform --vram-backing private-tmpfs '
                     '--serial-device-pm, without --kernel-trace')
    guard_bypass = (('vram-tmpfs' if args.vram_backing == 'private-tmpfs' else 'vram-current') if args.vram_backing else
                   'graphics-only' if args.graphics_only else 'no-graphics' if args.no_graphics else
                    ("cardwire-off" if args.cardwire_off_guard_bypass else args.host_reset_guard_bypass))
    if args.serial_device_pm:
        guard_bypass = RTC_MODE if args.rtc_pm_trace else SERIAL_MODE
    ends_graphics = args.no_graphics or args.graphics_only
    if args.allow_logout and (not ends_graphics or args.action not in ('run', '_worker')):
        parser.error('--allow-logout is only valid for run --no-graphics or --graphics-only')
    if ends_graphics and args.action in ('run', '_worker') and not args.allow_logout:
        parser.error('This graphics test requires --allow-logout after saving work')
    if args.graphics_only and (args.stage or args.action not in ('check', 'run', '_worker')):
        parser.error('--graphics-only accepts check/run without any --stage or PM bypass')
    if args.vram_backing and args.action not in ('check', 'run', '_worker'):
        parser.error('--vram-backing supports only check/run with an explicit diagnostic stage (default: freezer)')
    if guard_bypass and not args.graphics_only and not args.vram_backing and (args.action not in ("check", "run", "_worker") or stage != "platform"):
        parser.error("A sleep-guard bypass requires check/run --stage platform")
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
        if args.graphics_only:
            info = no_graphics_module().preflight_graphics(sys.modules[__name__])
        else:
            info = (preflight(stage, guard_bypass, preparing=True) if args.no_graphics
                    else preflight(stage, guard_bypass))
        if args.kernel_trace:
            info['kernel_trace'] = trace_module().check()
        print(json.dumps(info, indent=2))
        print("READ-ONLY PREFLIGHT PASSED. No sleep requested and no configuration changed.")
        return 0
    if args.action == "run":
        return launch(stage, guard_bypass, **({'kernel_trace': True} if args.kernel_trace else {}))
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
    return worker(folder, stage, guard_bypass, kernel_trace=args.kernel_trace)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
