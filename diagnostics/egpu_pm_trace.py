#!/usr/bin/python3
"""Bounded, isolated PM trace collector. This module never requests sleep."""

import argparse
import json
import os
from pathlib import Path
import select
import signal
import threading
import time
import uuid


TRACEFS = Path('/sys/kernel/tracing')
EVENTS = ('power:suspend_resume', 'power:device_pm_callback_start',
          'power:device_pm_callback_end', 'notifier:notifier_run')
FUNCTIONS = ('pm_prepare_console', 'pm_notifier_call_chain_robust',
             'nv_pm_notifier', 'nv_set_system_power_state')
MAX_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 180


def check():
    """Read only; availability is not evidence that a PM cycle works."""
    events = set((TRACEFS / 'available_events').read_text().split())
    functions = {line.split()[0] for line in
                 (TRACEFS / 'available_filter_functions').read_text().splitlines() if line.strip()}
    missing = sorted(set(EVENTS) - events) + sorted(set(FUNCTIONS) - functions)
    if missing:
        raise RuntimeError('Missing trace targets: ' + ', '.join(missing))
    if 'function' not in (TRACEFS / 'available_tracers').read_text().split():
        raise RuntimeError('The function tracer is unavailable.')
    clocks = (TRACEFS / 'trace_clock').read_text().replace('[', '').replace(']', '').split()
    if 'mono' not in clocks:
        raise RuntimeError('The monotonic trace clock is unavailable.')
    if not (TRACEFS / 'instances').is_dir():
        raise RuntimeError('Trace instances are unavailable; never use the global buffer as fallback.')
    return {'events': list(EVENTS), 'functions': list(FUNCTIONS),
            'global_tracer': (TRACEFS / 'current_tracer').read_text().strip(),
            'global_tracing_on': (TRACEFS / 'tracing_on').read_text().strip(),
            'global_events_enabled': (TRACEFS / 'events/enable').read_text().strip(),
            'retention': 'userspace streaming only; no guarantee after freezing or forced reset'}


def durable_json(path, value):
    with path.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


class Capture:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.token = uuid.uuid4().hex
        self.instance = TRACEFS / 'instances' / ('egpu-pm-' + self.token)
        self.identity = None
        self.done = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.fd = None
        self.output = None
        self.error = None
        self.byte_count = 0
        self.marker = ('EGPU_PM_TRACE_BEGIN_' + self.token).encode()

    def owned(self):
        if self.identity is None or self.instance.is_symlink():
            raise RuntimeError('Trace instance ownership is unverified.')
        state = self.instance.stat()
        if self.identity != (state.st_dev, state.st_ino):
            raise RuntimeError('Trace instance identity changed; refusing to alter it.')

    def write(self, name, value):
        self.owned()
        # Names come exclusively from fixed constants below, never CLI input.
        (self.instance / name).write_text(value + '\n')

    def start(self):
        baseline = check()
        # Exclusive creation: never adopt, reset, or remove another instance.
        self.instance.mkdir()
        state = self.instance.stat()
        self.identity = (state.st_dev, state.st_ino)
        try:
            self.write('tracing_on', '0')
            self.write('current_tracer', 'nop')
            self.write('events/enable', '0')
            self.write('buffer_size_kb', '64')  # Per CPU, not global trace memory.
            self.write('trace_clock', 'mono')
            self.write('set_ftrace_filter', '\n'.join(FUNCTIONS))
            actual = {line.split()[0] for line in
                      (self.instance / 'set_ftrace_filter').read_text().splitlines() if line.strip()}
            if actual != set(FUNCTIONS):
                raise RuntimeError('Exact function filter was not accepted; refusing unfiltered tracing.')
            self.write('current_tracer', 'function')
            for event in EVENTS:
                self.write('events/' + event.replace(':', '/') + '/enable', '1')
            self.fd = os.open(self.instance / 'trace_pipe', os.O_RDONLY | os.O_NONBLOCK)
            self.output = (self.folder / 'kernel-trace.txt').open('xb', buffering=0)
            durable_json(self.folder / 'kernel-trace-config.json', {
                **baseline, 'instance': str(self.instance), 'buffer_kib_per_cpu': 64,
                'max_bytes': MAX_BYTES, 'max_seconds': MAX_SECONDS,
                'function_records': 'entry only; not function returns',
                'notifier_records': 'all notifier callbacks; correlate PID and PM phase',
                'started_monotonic': time.monotonic(),
            })
            self.write('tracing_on', '1')
            self.write('trace_marker', self.marker.decode())
            self.thread = threading.Thread(target=self.read, daemon=True)
            self.thread.start()
            if not self.ready.wait(5) or self.error:
                raise RuntimeError(self.error or 'Trace marker was not persisted; no PM test should follow.')
        except BaseException:
            self.stop()
            raise

    def ensure_running(self):
        self.owned()
        if (self.error or self.thread is None or not self.thread.is_alive()
                or (self.instance / 'tracing_on').read_text().strip() != '1'):
            raise RuntimeError(self.error or 'Trace collector stopped before the sleep request.')

    def save(self, chunk):
        if self.byte_count + len(chunk) > MAX_BYTES:
            raise RuntimeError('Trace byte limit reached; capture stopped, not evidence of PM completion.')
        # FileIO can return a short write; never acknowledge an unpersisted marker.
        remaining = memoryview(chunk)
        while remaining:
            count = self.output.write(remaining)
            if not count:
                raise OSError('Short trace output write')
            remaining = remaining[count:]
        os.fsync(self.output.fileno())
        self.byte_count += len(chunk)

    def read(self):
        deadline = time.monotonic() + MAX_SECONDS
        tail = b''
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Trace deadline reached; not a PM watchdog or completion signal.')
                try:
                    chunk = os.read(self.fd, 65536)
                except BlockingIOError:
                    if self.done.is_set():
                        break
                    select.select([self.fd], [], [], 0.2)
                    continue
                if not chunk:
                    if self.done.is_set():
                        break
                    raise RuntimeError('Trace pipe closed unexpectedly.')
                self.save(chunk)
                combined = tail + chunk
                if self.marker in combined:
                    self.ready.set()
                tail = combined[-len(self.marker):]
        except Exception as exc:
            self.error = str(exc)
            self.ready.set()
            # Stop only our instance. No PM/driver/guard action on capture errors.
            try:
                self.write('tracing_on', '0')
            except Exception:
                pass

    def stop(self):
        if self.identity is None:
            return
        # Stop recording before joining. Do NOT switch current_tracer while
        # trace_pipe is open: tracing_set_tracer rejects a live trace_ref with
        # EBUSY. A blocked reader retains its disabled instance and tracer.
        # Publish intent before tracing_on=0 can make trace_pipe return EOF.
        self.done.set()
        errors = []
        for name, value in (('tracing_on', '0'), ('events/enable', '0')):
            try:
                self.write(name, value)
            except Exception as exc:
                errors.append(str(exc))
        if self.thread:
            self.thread.join(timeout=3)
        live = self.thread is not None and self.thread.is_alive()
        if not live:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if self.output is not None:
                self.output.close()
                self.output = None
            try:
                self.write('current_tracer', 'nop')
            except Exception as exc:
                errors.append(str(exc))
            # Save ring overrun evidence before removal; streaming is lossy if
            # the reader freezes or cannot keep up. Never silently call it complete.
            if not errors:
                try:
                    self.owned()
                    stats = {p.parent.name: p.read_text() for p in
                             (self.instance / 'per_cpu').glob('cpu*/stats')}
                    durable_json(self.folder / 'kernel-trace-buffer-stats.json', stats)
                    self.instance.rmdir()  # tracefs removes virtual files; never recursive.
                    self.identity = None
                except Exception as exc:
                    errors.append(str(exc))
        else:
            errors.append('Trace reader still alive; instance retained, no forced termination.')
        durable_json(self.folder / 'kernel-trace-status.json', {
            'instance': str(self.instance),
            'bytes_persisted': self.byte_count, 'capture_error': self.error,
            'cleanup_errors': errors, 'instance_retained': self.identity is not None,
            'warning': 'Capture status is not a PM test result. Absence of an event does not prove it never ran.',
        })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'probe'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run on the host as root. Neither action requests sleep.')
    print(json.dumps(check(), indent=2))
    if args.action == 'check':
        print('READ-ONLY TRACE CHECK PASSED. No tracing or sleep requested.')
        return
    import tempfile
    root = Path('/var/log/egpu-sleep-lab')
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != 0 or root.stat().st_mode & 0o022:
        raise RuntimeError('Unsafe diagnostic log root')
    folder = Path(tempfile.mkdtemp(prefix='trace-probe-', dir=root))
    capture = Capture(folder)
    def interrupted(signum, frame):
        raise InterruptedError('Trace probe interrupted; no sleep requested.')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        capture.start()
        print(f'Trace marker persisted. NO SLEEP requested. Logs: {folder}', flush=True)
    finally:
        capture.stop()
    status = json.loads((folder / 'kernel-trace-status.json').read_text())
    if status['capture_error'] or status['cleanup_errors']:
        print(json.dumps(status, indent=2), flush=True)
        raise RuntimeError('Trace probe was not clean; inspect ' + str(folder))
    print('NO-SLEEP TRACE PROBE PASSED; private instance removed. This does not validate suspend.')


if __name__ == '__main__':
    main()
