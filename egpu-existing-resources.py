#!/usr/bin/env python3
"""Read-only validation of the exact no-dock PCI subtree; never write sysfs.

Address/bus windows may differ from the reservation profile, but identities,
parentage, BAR sizes, allocation flags, containment and isolation must hold.
"""
import argparse
import itertools
from pathlib import Path
import re
import sys

IO, MEM, PREF = 0x100, 0x200, 0x2000
BAD = 0x10000000 | 0x20000000  # IORESOURCE_DISABLED | IORESOURCE_UNSET


def require(condition, message):
    if not condition:
        raise ValueError(message)


def resources(path):
    rows = [tuple(int(v, 16) for v in line.split())
            for line in (path / 'resource').read_text().splitlines()]
    require(len(rows) >= 7 and all(len(r) == 3 for r in rows),
            f'{path.name}: malformed resource table')
    for start, end, flags in rows:
        if flags:
            require(start > 0 and end >= start and not flags & BAD,
                    f'{path.name}: unassigned/disabled/invalid resource {start:#x}-{end:#x}')
            require(flags & (IO | MEM) in (IO, MEM), f'{path.name}: unknown resource type')
    return rows


def overlap(a, b):
    return bool(a[2] and b[2] and (a[2] & (IO | MEM)) == (b[2] & (IO | MEM))
                and a[0] <= b[1] and b[0] <= a[1])


def contained(child, windows):
    start, end, flags = child
    if not flags:
        return True
    for lo, hi, wf in windows:
        # Prefetchable child allocations may live in ordinary memory windows;
        # non-prefetchable allocations may not use a prefetch-only window.
        if wf and (wf & (IO | MEM)) == (flags & (IO | MEM)):
            if not flags & PREF and wf & PREF:
                continue
            if lo <= start <= end <= hi:
                return True
    return False


def validate(base, root, upstream, bridge, gpu, audio, bridge_id, bar_sizes):
    base = Path(base)
    ports = [bridge.rsplit(':', 1)[0] + f':0{i}.0' for i in (1, 2, 3)]
    expected = {root, upstream, bridge, gpu, audio, *ports}
    paths = {bdf: (base / bdf).resolve(strict=True) for bdf in expected}
    parents = {upstream: root, bridge: upstream, gpu: bridge, audio: bridge,
               **{p: upstream for p in ports}}
    for child, parent in parents.items():
        require(paths[child].parent == paths[parent], f'{child}: unexpected PCI parent')
    descendants = {p.name for p in base.iterdir()
                   if p.resolve().is_relative_to(paths[root])}
    require(descendants == expected,
            f'no-dock test requires exact empty enclosure; unexpected/missing nodes: {descendants ^ expected}')
    for bdf in [upstream, bridge, *ports]:
        identity = ':'.join((paths[bdf] / attr).read_text().strip() for attr in ('vendor', 'device'))
        require(identity == bridge_id, f'{bdf}: wrong bridge identity')

    tables = {bdf: resources(path) for bdf, path in paths.items()}
    bridges = [root, upstream, bridge, *ports]
    buses = {}
    for bdf in bridges:
        require(int((paths[bdf] / 'class').read_text(), 16) >> 8 == 0x0604,
                f'{bdf}: not a PCI bridge')
        require(len(tables[bdf]) >= 16, f'{bdf}: missing bridge windows')
        sec = int((paths[bdf] / 'secondary_bus_number').read_text())
        sub = int((paths[bdf] / 'subordinate_bus_number').read_text())
        primary = int(bdf.split(':')[1], 16)
        require(primary < sec <= sub <= 255, f'{bdf}: invalid bus range')
        buses[bdf] = (sec, sub)
        for i, kind in ((13, IO), (14, MEM), (15, MEM | PREF)):
            flags = tables[bdf][i][2]
            require(not flags or flags & (IO | MEM | PREF) == kind,
                    f'{bdf}: incorrect bridge window type')

    for child, parent in parents.items():
        sec, sub = buses[parent]
        require(int(child.split(':')[1], 16) == sec, f'{child}: not on parent secondary bus')
        if child in buses:
            require(sec <= buses[child][0] <= buses[child][1] <= sub,
                    f'{child}: bus range escapes parent')
        for row in tables[child]:
            require(contained(row, tables[parent][13:16]),
                    f'{child}: resource {row[0]:#x}-{row[1]:#x} outside parent {parent}')

    for bdf, rows in tables.items():
        for a, b in itertools.combinations(rows, 2):
            require(not overlap(a, b), f'{bdf}: own resources overlap')
    for a, b in itertools.combinations(parents, 2):
        if parents[a] != parents[b]:
            continue
        if a in buses and b in buses:
            require(buses[a][1] < buses[b][0] or buses[b][1] < buses[a][0],
                    f'{a}/{b}: sibling bus ranges overlap')
        for ar in tables[a]:
            for br in tables[b]:
                require(not overlap(ar, br), f'{a}/{b}: sibling resources overlap')

    for bdf, index, size, kind in bar_sizes:
        start, end, flags = tables[bdf][index]
        require(flags and end - start + 1 == size and flags & (IO | MEM | PREF) == kind,
                f'{bdf} BAR{index}: expected {size} bytes/type {kind:#x}, got {end-start+1 if flags else 0}')
        require(start % size == 0, f'{bdf} BAR{index}: misaligned allocation')

    return [f'{bdf}: buses {buses[bdf][0]:02x}-{buses[bdf][1]:02x}' for bdf in bridges]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--devices', default='/sys/bus/pci/devices')
    for key in ('root', 'upstream', 'bridge', 'gpu', 'audio', 'bridge-id'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--bar-sizes', required=True, nargs=5, type=int,
                        metavar=('BAR0', 'BAR1', 'BAR3', 'BAR5', 'AUDIO0'))
    args = parser.parse_args()
    for bdf in (args.root, args.upstream, args.bridge, args.gpu, args.audio):
        require(re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]', bdf), 'Invalid BDF')
    specs = [(args.gpu, i, size, kind) for i, size, kind in
             zip((0, 1, 3, 5), args.bar_sizes[:4], (MEM, MEM | PREF, MEM | PREF, IO))]
    specs.append((args.audio, 0, args.bar_sizes[4], MEM))
    # Keep the actual layout in the boot journal even if validation fails.
    for bdf in (args.root, args.upstream, args.bridge, args.gpu, args.audio):
        path = Path(args.devices) / bdf
        if (path / 'secondary_bus_number').exists():
            print(f'{bdf}: secondary={(path / "secondary_bus_number").read_text().strip()} '
                  f'subordinate={(path / "subordinate_bus_number").read_text().strip()}', flush=True)
        for i, line in enumerate((path / 'resource').read_text().splitlines()):
            start, end, flags = (int(v, 16) for v in line.split())
            if flags:
                print(f'{bdf} resource[{i}]: {start:#x}-{end:#x} size={end-start+1} flags={flags:#x}', flush=True)
    for line in validate(args.devices, args.root, args.upstream, args.bridge,
                         args.gpu, args.audio, args.bridge_id, specs):
        print(line)
    print('EXISTING PCI RESOURCES VERIFIED: exact no-dock subtree, full RTX BAR1, contained and non-overlapping resources. No resources changed.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, IndexError) as error:
        print(f'EXISTING PCI RESOURCES FAILED: {error}', file=sys.stderr)
        sys.exit(1)
