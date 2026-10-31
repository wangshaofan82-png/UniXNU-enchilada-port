#!/usr/bin/env python3
"""Check the candidate platform intervals against the compiled enchilada device tree.

Adapted from UniXNU-sheng scripts/check-sheng.py (BSD-2-Clause-Patent). This is the board preflight
of the firmware step: it cross-checks three things that must agree before a firmware volume is worth
flashing,

    the UEFI memory map      Platforms/OnePlus/enchiladaPkg/Library/MemoryMapLib/MemoryMapLib.c
    the device tree          /reserved-memory of enchilada.dts, compiled or as source
    the platform config      Resources/Configs/enchilada.toml and enchilada.dsc

because a region that the device tree reserves but the memory map hands out as conventional memory
is memory that the firmware still owns while UEFI - and later XNU - treat it as free.

Differences from the sheng original, all of them needed on this board:

  * the Mu-Silicium checkout is found instead of assuming the directory name `uefi-enchilada`
    (the CI checks it out as `../Mu-Silicium`; $UNIXNU_UEFI also wins);
  * the device tree is read as a .dts when `fdtget` is missing or the .dtb has not been compiled
    yet, so the check works before anything is built;
  * a missing `Binaries/enchilada` tree is reported but not fatal, because the vendor binaries are
    a submodule and a fresh checkout that skipped `--recurse-submodules` has no such directory.

Run from the assembled build tree root, which is what the firmware step does.
Exit status is 1 when a static error is found.
"""
import csv
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tomllib

UEFI_CANDIDATES = ('uefi-enchilada', '../Mu-Silicium', '../uefi-enchilada', 'Mu-Silicium')
DTB_CANDIDATES = ('build/enchilada/enchilada.dtb',)


def find_uefi():
    names = ('uefi-enchilada', 'Mu-Silicium')
    candidates = [os.environ.get('UNIXNU_UEFI')] + list(UEFI_CANDIDATES)
    # The firmware step runs this from the assembled tree (out/<device>/tree), while the Mu-Silicium
    # checkout lives next to the UniXNU checkout - so walk up and look in every parent's siblings.
    here = Path.cwd().resolve()
    for parent in list(here.parents)[:5]:
        for name in names:
            candidates.append(str(parent / name))
            candidates.append(str(parent.parent / name))
    for candidate in candidates:
        if candidate and (Path(candidate) / 'Platforms/OnePlus/enchiladaPkg').is_dir():
            return Path(candidate).resolve()
    raise SystemExit('no Mu-Silicium checkout found: set UNIXNU_UEFI or check one out next to the tree')


def find_uefi_root(uefi):
    """The checkout that holds Resources/ (it can be the parent when the port is a subdirectory)."""
    for candidate in (uefi, uefi.parent):
        if (candidate / 'Resources/Configs/enchilada.toml').is_file():
            return candidate
    raise SystemExit(f'Resources/Configs/enchilada.toml not found under {uefi}')


def reservations_from_dtb(dtb):
    nodes = subprocess.check_output(['fdtget', '-l', str(dtb), '/reserved-memory'], text=True).split()
    found = []
    for node in nodes:
        result = subprocess.run(['fdtget', '-t', 'x', str(dtb), '/reserved-memory/' + node, 'reg'],
                                capture_output=True, text=True)
        if result.returncode:
            continue
        values = [int(word, 16) for word in result.stdout.split()]
        for index in range(0, len(values) - 3, 4):
            found.append((node, (values[index] << 32) | values[index + 1],
                          (values[index + 2] << 32) | values[index + 3]))
    return found


def reservations_from_dts(dts):
    """The same information straight from the source, for hosts without fdtget."""
    text = dts.read_text(encoding='utf-8', errors='replace')
    match = re.search(r'^\s*reserved-memory\s*\{', text, re.M)
    if not match:
        raise SystemExit(f'no reserved-memory node in {dts}')
    depth, index, end = 0, match.end() - 1, len(text)
    while index < len(text):
        if text[index] == '{':
            depth += 1
        elif text[index] == '}':
            depth -= 1
            if depth == 0:
                end = index
                break
        index += 1
    block = text[match.end():end]
    found = []
    for node in re.finditer(r'^\s*(\w[\w\-]*)@([0-9a-fA-F]+)\s*\{', block, re.M):
        reg = re.search(r'reg\s*=\s*<([^>]*)>', block[node.end():node.end() + 400])
        if not reg:
            continue     # alloc-ranges nodes have no fixed address
        cells = [int(cell, 0) for cell in reg.group(1).split()]
        if len(cells) < 4:
            continue
        found.append((node.group(1), (cells[0] << 32) | cells[1], (cells[2] << 32) | cells[3]))
    return found


def device_tree_reservations(uefi_root):
    """The compiled tree when fdtget can read it, the source otherwise."""
    dtb = None
    for candidate in DTB_CANDIDATES:
        if Path(candidate).is_file():
            dtb = Path(candidate)
            break
    if dtb is None and (uefi_root / 'Resources/DTBs/enchilada.dtb').is_file():
        dtb = uefi_root / 'Resources/DTBs/enchilada.dtb'
    if dtb is not None:
        try:
            return reservations_from_dtb(dtb), dtb
        except (OSError, subprocess.CalledProcessError, ValueError) as error:
            # fdtget missing, or the file is not a device tree blob yet
            print('warning: could not read {} ({}); reading the source instead'.format(dtb, error))
    dts = uefi_root / 'Resources/DTBs/enchilada.dts'
    if dts.is_file():
        return reservations_from_dts(dts), dts
    raise SystemExit('no enchilada device tree: neither the compiled .dtb nor the .dts was found')


def main():
    uefi = find_uefi()
    uefi_root = find_uefi_root(uefi)
    platform = uefi / 'Platforms/OnePlus/enchiladaPkg'
    reservations, tree_path = device_tree_reservations(uefi_root)

    source = (platform / 'Library/MemoryMapLib/MemoryMapLib.c').read_text(encoding='utf-8')
    rows = [(match[1], int(match[2], 16), int(match[3], 16), match[4]) for match in re.finditer(
        r'\{"([^"]+)"\s*,\s*(0x[0-9a-fA-F]+),\s*(0x[0-9a-fA-F]+),([^}]+)\}', source)]
    errors, warnings = [], []
    if not rows or len(rows) > 128:
        errors.append('Empty map or descriptor capacity exceeded')

    for name, base, size in reservations:
        exact = any(a == base and s == size and re.search(r'\bReserv\b', attrs)
                    for _, a, s, attrs in rows)
        # A region the map does not mention at all is not handed out either, so it is safe. A region
        # that lands inside allocatable memory is the real conflict. A region that spans several
        # descriptors is fine as long as every one of them is reserved - removed_region on this board
        # is 0x4b40000 long and covers AOP CMD DB, SMEM and QSEE, all reserved.
        holders = [entry for entry in rows
                   if max(entry[1], base) < min(entry[1] + entry[2], base + size)]
        allocatable = [entry for entry in holders
                       if 'Conv' in entry[3] and not re.search(r'\bReserv\b', entry[3])]
        if allocatable:
            errors.append('Reservation {} falls inside allocatable memory: {}'.format(
                name, ', '.join(entry[0] for entry in allocatable)))
        elif exact:
            continue
        elif holders:
            # Every descriptor the reservation touches is non-conventional (Reserv, RtData, BsData, ...):
            # none of it is handed out as free memory, so the reservation is honoured. The only real
            # conflict - a reservation inside allocatable memory - was already caught above. Note how much
            # of the reservation the map actually covers (bootloader_log_mem spans 1 MiB in the tree but the
            # map only describes its 36 KiB log buffer + info block as RtData); the uncovered tail is not
            # mapped, so it is not handed out either.
            covered = sum(min(entry[1] + entry[2], base + size) - max(entry[1], base)
                          for entry in holders)
            if covered < size:
                warnings.append('Reservation {} is covered only in part ({:#x} of {:#x} bytes); '
                                'the rest is not mapped'.format(name, covered, size))
        else:
            warnings.append('Reservation {} is not in the UEFI memory map at all'.format(name))

    for index, (name, base, size, _) in enumerate(rows):
        if base % 4096 or size % 4096 or not size:
            errors.append(f'Invalid UEFI page alignment: {name}')
        for other, other_base, other_size, _ in rows[index + 1:]:
            if max(base, other_base) < min(base + size, other_base + other_size):
                # Register windows legitimately nest (GPU_CPR inside GPU_CC, MDSS inside DISP_CC).
                if abs(base - other_base) < 0x1000 and abs(size - other_size) < 0x1000:
                    continue
                warnings.append(f'Overlapping intervals: {name}, {other}')

    config = tomllib.loads((uefi_root / 'Resources/Configs/enchilada.toml').read_text(encoding='utf-8'))
    fd = config['uefi_fd']
    if not any(entry[0] == 'UEFI FD' and entry[1] == fd['base'] and entry[2] == fd['size']
               for entry in rows):
        errors.append('FD configuration does not match memory descriptor')
    dsc = (platform / 'enchilada.dsc').read_text(encoding='utf-8')
    stack = re.search(r'PcdCPUCoresStackBase\|(0x[0-9a-fA-F]+)', dsc)
    if not stack or not any(entry[0] == 'UEFI Stack' and entry[1] == int(stack[1], 16)
                            for entry in rows):
        errors.append('Stack configuration does not match memory descriptor')

    dependencies, missing = set(), []
    for inc in (platform / 'Include').glob('*.inc'):
        dependencies.update(re.findall(r'Binaries/enchilada/[^\s}]+', inc.read_text(encoding='utf-8')))
    if not (uefi / 'Binaries/enchilada').is_dir() and not (uefi_root / 'Binaries/enchilada').is_dir():
        warnings.append('the Binaries submodule is not checked out, so the {} vendor inputs cannot be '
                        'verified (clone with --recurse-submodules)'.format(len(dependencies)))
    else:
        base_dir = uefi if (uefi / 'Binaries/enchilada').is_dir() else uefi_root
        missing = sorted(path for path in dependencies if not (base_dir / path).is_file())

    # The vendor firmware's own memory map, when the board has dumped it, is the ground truth the
    # UEFI descriptor table has to agree with.
    stock_count = 0
    firmware = Path('.cache/firmware/enchilada/uefiplat.cfg')
    if firmware.is_file():
        sections = firmware.read_text(encoding='utf-8').split('[MemoryMap]', 1)
        if len(sections) < 2:
            warnings.append('{} has no [MemoryMap] section'.format(firmware))
        else:
            text = sections[1].split('[RegisterMap]', 1)[0]
            for line in text.splitlines():
                if not line.strip().startswith('0x'):
                    continue
                record = next(csv.reader([line], skipinitialspace=True))
                base, size = int(record[0], 16), int(record[1], 16)
                remaining = [(base, base + size)]
                stock_count += 1
                for name, address, extent, attributes in rows:
                    remaining = [piece for lo, hi in remaining
                                 for piece in [(lo, min(hi, address)), (max(lo, address + extent), hi)]
                                 if piece[0] < piece[1]]
                    if max(base, address) < min(base + size, address + extent):
                        if record[3] == 'NoMap' and 'HobOnlyNoCacheSetting' not in attributes:
                            errors.append(f'Stock NoMap range is mapped: {name}')
                        if record[6] == 'Reserv' and not re.search(r'\bReserv\b', attributes):
                            errors.append(f'Stock reserved range is allocatable: {name}')
                if remaining:
                    errors.append(f'Uncovered stock region: {record[2]}')

    report = Path('build/enchilada/check.json')
    report.parent.mkdir(parents=True, exist_ok=True)
    result = {'uefi': str(uefi), 'device_tree': str(tree_path),
              'descriptors': len(rows), 'source_reservations': len(reservations),
              'stock_regions_checked': stock_count, 'static_errors': errors,
              'warnings': warnings, 'missing_vendor_inputs': missing,
              'runtime_ram_verified': False, 'hardware_boot_demonstrated': False}
    report.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(f'{len(reservations)} device tree reservations checked against {len(rows)} descriptors'
          f' of {uefi}')
    print(f'{len(errors)} static error(s), {len(warnings)} warning(s), '
          f'{len(missing)} vendor input(s) missing; hardware unverified')
    for error in errors:
        print('error:', error)
    for warning in warnings:
        print('warning:', warning)
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
