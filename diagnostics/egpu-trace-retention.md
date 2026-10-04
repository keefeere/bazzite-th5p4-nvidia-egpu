# Reserved-RAM marker retention — no sleep

This separate diagnostic prepares an evidence path beyond userspace freezing.
It is **not a suspend fix or permission to retry the hanging platform test**.
On Sep 19, the user-supplied before/after traces confirmed one marker survived
an ordinary warm reboot at the same reserved address. The first verifier
reported a false negative because the callsite label changed from
`tracing_mark_write` to `kallsyms_offsets`. The marker payload, PID, CPU and
timestamp were unchanged. The user reran the corrected verifier in recovery boot
`43a59cbc-dc88-49c1-8bae-9c2c709d08a3`: `marker_present`, `same_record` and
`retained` were all true; `symbol_changed` was true. No second marker or extra
reboot was needed. This is not forced-reset or PM validation.

The last traced platform attempt reached `freeze_processes` after advancing
past NVIDIA's prepare notifier. The userspace reader then stopped producing
evidence. A last record at freeze entry does not locate the hang. See the
[recorded results](2026-09-18-suspend-status.md).

## Scope

`egpu_trace_retention.py` is restricted to the inspected
`7.2.4-ogc3.1.fc44.x86_64` kernel. It stores the actual kernel-image SHA-256 and
requires that image, the staged OSTree checksum and unchanged unrelated boot
arguments during the comparison. It does not change the loader's old/new kernel
branches. It is not installed or invoked by the production eGPU installer.

Only `stage` and `cancel` edit next-boot arguments, with these exact values:

```text
reserve_mem=16M:0x2000000:egpu_pmtrace
trace_instance=egpu_pm_retention^traceoff@egpu_pmtrace
```

The kernel chooses the physical address: no physical address is guessed and
KASLR is not disabled. The named reservation is 16 MiB, aligned to 32 MiB;
trace recording starts off. This uses an existing kernel facility, not a
kernel rebuild. The inspected image contains the parameter strings; successful
buffer creation still needs verification after boot.

Only `mark` enables recording briefly to write one unpredictable marker, then
disables it in a `finally` block. It checks the kernel's mapping message, size,
alignment and the mapped instance's `buffer_meta` files first. Existing records,
enabled events, a non-`nop` tracer, trace-printk routing or configured event
triggers cause refusal. It never clears trace data, changes a tracer/clock/buffer
size, consumes `trace_pipe`, removes an instance, or changes global tracing.

There are **no** suspend requests, guard bypasses, logout/service-stop actions,
GPU/PCI resets, module operations, deployment finalization, automatic reboots,
or automatic retry. Ordinary sleep remains blocked by the installed guard.

## Staged procedure

Run from the repository root. Do not combine this with an update, other boot
argument changes, another tracing experiment or a PM test. Keep the dock/cable
configuration unchanged. Inspect each result before the next step; do not run
this sequence as an unattended batch.

1. Read-only preflight (no files or arguments are changed):

   ```bash
   sudo python3 diagnostics/egpu_trace_retention.py check
   ```

2. **Only after deciding to stage the next-boot experiment:**

   ```bash
   sudo python3 diagnostics/egpu_trace_retention.py stage
   ```

   This refuses a pending/default-unbooted deployment or an active OSTree
   transaction. It saves ownership before requesting the two argument additions.
   Save work, then perform an ordinary warm reboot yourself. Do not power-cycle
   or request sleep. If boot fails, select the previous deployment in the boot
   menu; do not repeatedly boot the failing experimental entry.

3. After the first reboot, confirm the desktop/eGPU is healthy, then:

   ```bash
   sudo python3 diagnostics/egpu_trace_retention.py mark
   ```

   Continue only if the marker was confirmed and recording is off. A failed
   `mark` must be inspected, not retried with manual trace clearing. Do not
   cancel or update yet: both retention arguments must remain for recovery.
   Save work and perform a second ordinary warm reboot into this same deployment.

4. Read back the marker without consuming trace data:

   ```bash
   sudo python3 diagnostics/egpu_trace_retention.py verify
   ```

   A successful result requires a **different boot ID**, unchanged kernel and
   reserved address/size, and the exact marker in the live RAM trace with the
   same PID, CPU and timestamp as the original archived marker record. The
   callsite symbol and task name are not used as cross-boot identity. It never
   substitutes the saved text file for a missing live marker. Same-boot
   visibility is not retention. A missing or relocated buffer is not a pass.
   Changed verification results retain the earlier result in
   `verification_history`; raw archives are not rewritten.

5. When finished, stage removal of this experiment's two arguments:

   ```bash
   sudo python3 diagnostics/egpu_trace_retention.py cancel
   ```

   Cancellation is also available before the first reboot. It does not erase
   the current trace or saved evidence. A subsequent ordinary reboot releases
   the reservation; cancellation is not applied live. If another update or
   argument change has appeared, the tool refuses to modify it.

**This is not one-shot.** A forced reset does not guarantee OSTree has finalized
a pending argument change. No forced-reset or sleep test is part of this plan.
An ordinary reboot is needed to activate/remove these early-boot parameters;
live package application cannot activate a reserved early-boot buffer.

## Evidence, interruption and recovery

The root-only directory `/var/lib/egpu-trace-retention` retains `state.json`,
the original boot arguments, kernel hash, boot IDs, physical mapping, marker,
before/after marker text and `recovered-<boot-id>.txt`. Treat these as local
diagnostic evidence; review before publishing. `status` reads the saved record:

```bash
sudo python3 diagnostics/egpu_trace_retention.py status
```

The directory is never overwritten for a new experiment. Cancellation retains
it, so starting another experiment requires review/archiving rather than deleting
history. Never remove tracefs directories or `/var/lib` trees by wildcard.

If staging is interrupted after OSTree accepted the arguments but before its
target checksum was saved, ownership intent survives but the pending deployment
is unconfirmed. Automatic `cancel` deliberately refuses that unknown pending
state. Inspect `status`, `rpm-ostree status` and `rpm-ostree kargs` together before
an explicit recovery. Do not delete the record to bypass that check. Likewise,
a marker-write failure retains its attempted marker/phase; it cannot silently
create a second, ambiguous experiment.

If the old deployment was selected for recovery, `cancel` may remove the exact
owned pair from a recognized pending/default test deployment. If an unrelated
deployment or argument change has intervened, stop for inspection. The tool
does not roll back, delete, finalize or boot deployments for you.

## Interpretation and limits

An ordinary-reboot retention pass proves **only that marker survived that
reboot**. It does not prove retention across the forced-reset path used after
a hang, power loss, successful sleep, or correct decoding of every future PM
record. `reserve_mem` can choose a different address with KASLR; firmware can
clear RAM. Failure here is useful: do not spend another PM hang expecting this
capture method to save evidence.

The observed symbol mismatch is a decoding caveat, **not evidence that the
`kallsyms_offsets` function ran** or that marker contents changed. Future
cross-boot PM/function traces need independent validation of symbol decoding;
a text-marker pass does not validate arbitrary saved instruction addresses.

Any later PM capture needs a separately reviewed recorder for this boot-created
instance. Do **not** point `egpu_pm_trace.py` at it: that helper owns ephemeral
instances, streams from `trace_pipe` and removes its instance during cleanup.
This marker script records no PM events and has no integration with the lab.

## Decoding metadata preparation — still no sleep

`egpu_persistent_trace.py` currently provides **metadata collection only**, not
a PM recorder. Its name describes the intended diagnostic path; there is no
`run`, `record`, sleep request or lab integration yet. It is not installed by
the production installer. A successful marker check is not enough to trust
future cross-boot PM records: in the inspected kernel's `power.h`,
`suspend_resume.action` is a pointer, whereas the device-PM events copy their
device and driver strings into the record.

After the corrected retention verifier has passed in the current boot, use:

```bash
sudo python3 diagnostics/egpu_persistent_trace.py check
```

This reads the live marker, validates its original PID/CPU/timestamp and the
unchanged kernel/deployment/mapping, and requires tracing still off with no
events or triggers enabled. It writes no files or trace/PM settings. It also
requires the existing sleep guard, no active PM/lab job and visible required
kernel symbols; it refuses masked or ambiguous addresses rather than changing
`kptr_restrict`, lockdown or any security policy.

To save those metadata in a new private local directory, without another reboot:

```bash
sudo python3 diagnostics/egpu_persistent_trace.py bundle
```

This repeats the checks and creates
`/var/log/egpu-sleep-lab/persistent-meta-<random>/`. It saves:

- the original and currently recovered marker text and retention state copy;
- `last_boot_info`, trace clock, per-CPU statistics and buffer metadata;
- current-boot `/proc/kallsyms`, `/proc/modules`, `printk_formats`, event/header
  formats and the available selected function names, including compiler clones;
- a manifest containing boot/kernel/mapping identity and each file's size/hash.

Collection is bounded and rechecks marker text, module layout, event formats,
ownership state and the live deployment/guard. Directories are private (0700),
files 0600 and exclusive: existing bundles are never overwritten. An incomplete
bundle remains for inspection and is not reported as a pass. These files contain
kernel addresses and local machine details; do not publish the whole directory
unreviewed. The printed summary and bundle path are sufficient for the next step.
The summary separately reports whether `last_boot_info` supplies a nonzero prior
kernel base. A retained marker and a nonempty `last_boot_info` file do not imply
that its relocation metadata are usable.

Neither action changes or consumes trace data, uses `trace_pipe`, enables events,
modifies the guard, changes boot arguments or authorizes sleep. `bundle` also
uses the retention tool's local ownership lock; the retention state itself stays
unchanged. Neither action requires `trace-cmd`. Its absence is reported, not
treated as permission to install a package or alter the control deployment.

The metadata are a baseline for a **future same-boot recording**, not a claim
that current-boot symbol addresses decode the prior boot. Raw extraction and
offline decoding still require separate validation. In particular, do not turn
on `options/raw`/`hex` and assume all TRACE_EVENT payloads are exported: events
without those output handlers can print only their event type. Likewise,
`trace-cmd extract` reads consuming buffer interfaces; it must not be run against
the sole retained evidence before a reviewed export plan. `trace-cmd restore`'s
saved-header workflow is tested offline below; actual buffer export and PM
recorder integration remain separate work.

## Offline decoder check (synthetic records only)

The Sep 19 live bundle `persistent-meta-s4cfbtus` was copied privately for local
analysis with its original root-owned copy left intact. All 49 content hashes
matched. Its `last_boot_info` was exactly `0\t[kernel]\n`, without prior module
addresses. CPU 13 held the single marker, with zero overrun, dropped events or
read events. Thus the payload-retention result stands, but automatic cross-boot
symbol correction has no usable previous kernel base in this sample.

In upstream `trace.c`, `update_last_data()` initializes current kernel/module
metadata **and clears previous-boot records** when tracing is initialized for
new recording. This is consistent with a marker-only probe lacking initialized
relocation metadata; the exact installed-kernel cause has not been independently
confirmed. Do not toggle a tracer/event to "fix" old evidence: that can discard
it. Archive first, then explicitly begin a new recording generation. Preserve
that generation's kallsyms, modules, printk strings and event formats before PM.

`egpu-trace-offline-check.py` now validates one part of that approach **without
accessing live tracing or requesting PM**. It requires an unprivileged private
bundle copy, validates hashes and field layouts, and writes a new private
`/var/tmp/egpu-trace-offline-*` directory. It generates five clearly synthetic
records: a marker, a function call, a PM phase with a string pointer, and device
callback start/end. It uses `trace-cmd restore -c -t <saved-metadata> -k
<saved-kallsyms>`, then `restore -i` and `report -N` to decode those fixtures.
A negative control changes only saved symbol/string names in derivative fixture
files and confirms that the report follows those saved names rather than live
host metadata. The source bundle stays unchanged.

This passed with the actual captured formats/symbol table on Sep 19, using
separate unprivileged local builds (no system package installation):

- trace-cmd 3.4: `3ce20923b3efa60d417da7acc8a327615fbd1419`;
- libtraceevent 1.9.0: `13701b5532e0c3295bf5670361692b0d0044228d`;
- libtracefs 1.8.3: `6fad6a14ba0d4c4b437d9e4eed7098d4bb07b4fc`.

For a separately prepared toolchain, the offline invocation is:

```bash
env LD_LIBRARY_PATH=/path/to/private-prefix/lib \
  python3 diagnostics/egpu-trace-offline-check.py /path/to/private-bundle-copy \
  --trace-cmd /absolute/path/to/trace-cmd
```

Do not use sudo for this checker. The local library installs used only a private
prefix, an explicitly local pkgconfig directory and `LDCONFIG=false`; they did
not modify the host library loader, packages or deployment. This is not a request
to change libraries on the OS or add those binaries to the privileged production
stack. The tool's `extract`, `start`, `reset` and other live commands were not run.

Passing fixture decoding proves neither real PM capture, raw buffer extraction,
retention after forced reset nor working sleep. The next gate is a separately
reviewed no-sleep recorder/export probe of the owned reserved buffer, with the
old evidence archived and deliberate initialization of the new generation.
Do not wire the ephemeral trace helper into the persistent instance or rerun a
platform test merely because this offline check is green.

Primary references:

- [Linux persistent-buffer prerequisites, address stability and traceoff](https://docs.kernel.org/trace/debugging.html#persistent-buffers-across-boots)
- [Linux trace implementation: boot mapping and buffer_meta](https://github.com/torvalds/linux/blob/master/kernel/trace/trace.c)
- [Linux trace_print_print: callsite symbol rendered separately from payload](https://github.com/torvalds/linux/blob/master/kernel/trace/trace_output.c)
- [Linux reserve_mem allocator](https://github.com/torvalds/linux/blob/master/mm/memblock.c)
- [Linux PM event fields](https://github.com/torvalds/linux/blob/master/include/trace/events/power.h)
- [trace-cmd extract: instance selection and extraction](https://www.trace-cmd.org/Documentation/trace-cmd/trace-cmd-extract.1.html)
- [trace-cmd restore: saved headers and offline reconstruction](https://www.trace-cmd.org/Documentation/trace-cmd/trace-cmd-restore.1.html)

Upstream source explains the mechanism; it is not a claim that upstream master
is byte-for-byte the installed Bazzite kernel.

## No-sleep recording/export probe — validated on the host

`egpu_persistent_probe.py` is a separate, opt-in diagnostic, not a PM runner or
production service. The user separately authorized its one-shot run on Sep 19;
it has already consumed that authorization and is not repeatable on the same
state. The read-only preflight command was:

```bash
sudo python3 diagnostics/egpu_persistent_probe.py check
```

This requires the verified marker in this same boot, the exact kernel/deployment,
guard with no bypass, unchanged reserved mapping, idle trace controls, no custom
filters or CPU mask, no active global recorder, readable VFS read symbol and event
format, and at least 128 MiB free for archives. Missing prerequisites cause
refusal, not a fallback to another function, different buffer or kernel setting.
This stage needs neither closing applications nor logging out.

After inspection and separate approval, its single-use action was:

```bash
sudo python3 diagnostics/egpu_persistent_probe.py run
```

Execution deliberately replaces the old generation of **only** the owned
`egpu_pm_retention` instance. It first durably archives the old marker text,
original retention state and decoding metadata in a new private
`/var/log/egpu-sleep-lab/persistent-probe-*/before` directory. This archives the
old marker as text plus metadata, **not its raw ring pages**; the earlier original
marker archives remain unchanged. The state phase changes before any trace write,
so neither another run nor the old retention verifier can silently reuse it.
The original verification remains historical evidence, not a claim that its
marker is still live. Existing `status` and exact-argument `cancel` remain usable.

The probe configures an exact `vfs_read` function filter and
`syscalls:sys_enter_read` event, both scoped to its own PID, before enabling
recording. It writes two unique markers around eight ordinary `/dev/zero` reads
calls. No PM function is invoked, no PM event enabled, and no sleep-guard bypass
created. A 10-second process timer bounds normal recording; `finally` disables
it on handled errors/signals. SIGKILL, a kernel hang or power loss cannot be
guaranteed to run cleanup. Do not start sleep or another tracing experiment.

Export runs with recording off. It consumes only that instance's nonblocking
per-CPU `trace_pipe_raw` interfaces, saves complete 4096-byte pages (including
empty files for CPUs with no records), and checks loss/drain counters. Time and
byte bounds, private files, checksums and fsync protect against accidental
unbounded or ambiguous output. A short page is retained as partial evidence,
never padded and called valid. Readable trace text is also saved before raw
consumption. **This action is not read-only and empties these test records.**

Only after durable raw/metadata export does it select `nop` and clear its narrow
filters. Changing the tracer may clear buffers, so an export failure leaves the
instance OFF with its narrow tracer/filter retained, not reset. A failed run
requires inspection; there is no automatic retry or automatic PM/reboot step.
Global tracing, buffer size, clock, PCI resources, driver state and the desktop
session are not modified. See the [kernel tracing interface semantics](https://docs.kernel.org/trace/ftrace.html).

Successful raw export is still **not** successful decoding. Make a private,
unprivileged copy of the new `capture` directory for inspection (not the older
metadata bundle), then use the already-reviewed local toolchain:

```bash
env LD_LIBRARY_PATH=/path/to/private-prefix/lib \
  python3 diagnostics/egpu-trace-offline-check.py /path/to/private-capture-copy \
  --recorded --trace-cmd /absolute/path/to/trace-cmd
```

The decoder rejects root, checks both manifests and every checksum, retains
numeric CPU ordering including empty files, reconstructs the trace with saved
headers/symbols and `restore`, then uses `report -N`. A second decode of those
same raw pages renames only `vfs_read` in a derivative saved-symbol table; its
report must resolve the function under that new name. It never uses live
`extract`, `start` or `reset`. Both unique markers and the same-PID syscall and
function records must decode. No user-owned trace-cmd binary is run as root by
the probe. Keep full kallsyms/raw bundles private.

Even both green stages establish only same-boot no-sleep recording and export.
They do not establish forced-reset retention, locate the PM hang, or authorize
another platform test. Those remain separate gates; ordinary sleep stays guarded.

### Sep 19 no-sleep probe result

On boot `43a59cbc-dc88-49c1-8bae-9c2c709d08a3`, the one-shot probe
archived the retained marker, recorded eight `/dev/zero` reads, exported all
16 per-CPU buffers, and left tracing off. The result reported
`raw_export_complete: true`, with no capture or cleanup errors. An unprivileged
private-copy decode reported `real_capture_decoded: true`; its report contained
both unique markers, eight `sys_enter_read` events and eight `vfs_read`
function records from the probe PID. The root-only original is under
`/var/log/egpu-sleep-lab/persistent-probe-9jkhy34n/`; the decoded private
copy is under `/var/tmp/egpu-trace-decoded-kefuwpm4/`. The saved-symbol
negative control also passed on this real capture, confirming that this
same-boot decode uses the archived kallsyms rather than current `/proc` data.
These paths contain
machine-specific addresses and must not be published unreviewed.

This validates same-boot no-sleep raw capture and offline decoding only. It
does not establish retention of a newly recorded generation across reboot or
forced reset. No PM transition was requested, and the sleep guard remains in
place.

Read-only host inspection in the same boot found `/sys/fs/pstore` mounted but
`/sys/module/pstore/parameters/backend` set to `(null)` and EFI pstore disabled
(`pstore_disable=Y`). Thus pstore is not currently a usable fallback for
post-reset evidence. These settings were not changed. The reserved trace
buffer remains the candidate to validate with a newly recorded generation,
first across an ordinary reboot and only later, if warranted, across a forced
reset; neither transition has been requested for this generation.

`egpu_reboot_probe.py` is a separate one-shot test for that next experiment.
Its `check` action is read-only: it requires the previous one-shot export to
be complete and tied to this boot, the same mapped reserved instance to be
empty/idle, clean filters, unchanged global tracing, the installed guard and
enough free space. Passing `check` does not create a new generation, request a
reboot or permit sleep. Run it as root only when convenient:

```bash
sudo python3 diagnostics/egpu_reboot_probe.py check
```

The prepared `arm` action archives the empty generation before changing trace
controls, writes a fresh pair of unique markers around eight harmless reads,
captures same-boot decoding metadata, disables recording and leaves the new
records in the RAM buffer. It does **not** call reboot or PM. It is single-use:
an interrupted arm is marked failed, not silently retried. The separately
prepared `verify` action requires a different boot ID, the same kernel,
deployment and mapped reservation, compares both marker payloads/PID/CPU/time
before consuming raw pages, then exports them with the saved old-boot metadata.
The existing unprivileged `--recorded` decoder is the final gate. Neither
action has been run on the host, and a fixture test is not evidence of
cross-reboot survival. Do not run `arm` until `check` succeeds and the exact
workflow is reviewed; do not request sleep during this experiment.

When the operator is ready for **one ordinary reboot** with the same powered
eGPU chain and no OS update, the controlled sequence is:

1. Run `check` and inspect its result. On failure, stop; do not manually clear
   tracefs or bypass a preflight condition.
2. With separate confirmation, run
   `sudo python3 diagnostics/egpu_reboot_probe.py arm`. Continue only if it
   prints `NO-SLEEP generation armed, tracing OFF`. An interrupted/failed arm
   retains its state and evidence; there is no automatic retry.
3. Save work and perform a normal warm reboot into the **same** deployment.
   Do not sleep, update, cancel the two retention boot arguments or select a
   different deployment between arm and verify. The script never initiates
   this reboot.
4. After boot, run `sudo python3 diagnostics/egpu_reboot_probe.py verify`.
   It snapshots marker text *before* any consuming raw read. If it rejects
   missing/different markers, the snapshot remains on disk and it leaves the
   raw buffer untouched. An existing snapshot is accepted only if its boot ID
   and contents match exactly; it is never overwritten. Once raw
   export begins it becomes single-use; preserve a partial folder on error.
5. Copy only the new `capture` directory privately to user storage and run
   `egpu-trace-offline-check.py --recorded` on that copy with the separately
   built `trace-cmd`. A green `verify` without a green offline decode is not
   a complete cross-reboot result. Keep raw/kallsyms bundles private.

Even a green result here proves only ordinary-reboot retention of a new
no-sleep generation. It does not justify a forced reset or a platform suspend
attempt without a separately reviewed PM recorder and recovery procedure.

On Sep 19 the user-approved `arm` completed in boot
`43a59cbc-dc88-49c1-8bae-9c2c709d08a3`. Its root-only evidence is
`/var/log/egpu-sleep-lab/persistent-reboot-probe-i5lktkd5/`. The armed
snapshot had 18/18 entries: two markers and eight read-event/function pairs.
Recording and the read event were OFF before the user performed an ordinary
warm reboot into the same kernel/deployment.

The subsequent boot `dd998587-3489-451e-8e07-4ea17be3d921` mapped the
same 16 MiB reservation at `0x644000000`, but the kernel logged
`Ring buffer boot meta mismatch of magic`. The recovered snapshot has a `nop`
tracer and 0/0 entries; both markers are absent. `verify` retained the text
snapshot without consuming raw pages and set ownership phase
`reboot-probe-not-retained`. The first marker-only reboot pass did not
generalize to this new generation. The kernel's validator rejected the stored
buffer metadata; the exact cause of the invalid magic is not established.
Do not arm again or request a PM test on the expectation that this buffer
will preserve post-reset evidence. A different retention path would require
its own no-sleep validation first.
