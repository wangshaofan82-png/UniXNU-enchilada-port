#!/usr/bin/env python3
"""Patch the official Mu-Silicium OnePlus 6 (enchilada) port so a UEFI image can boot UniXNU.

Mu-Silicium already ships Platforms/OnePlus/enchiladaPkg (Status: Active, display/UFS/buttons/USB
working on hardware), so nothing is derived from another device here: the official package is
patched in place, idempotently.

What is added:
  * UniXNUPkg Application/UniXNU + Driver/UniXNUDxe into the firmware volume, and a
    UniXNUPkg boot option, so the boot manager runs from the FV (BDS finds it, no disk needed);
  * DarwinBootPkg/Driver/DarwinBoot into the firmware volume. UniXNU reaches XNU through
    gUniXNULoaderProtocolGuid, and DarwinBoot is what installs it; board.toml's
    [payload] loader = "firmware" says exactly that, so without this line the boot manager
    answers "xnu: no loader protocol installed; UniXNU does not provide one" and stops. The
    loader cannot come from the payload instead unless loader names the file, which is how the
    sibling ports that keep DarwinBoot.efi on the FAT volume do it;
  * enchiladaPkg/Drivers/EnchiladaBootMemoryDxe: the boot image ramdisk (the payload FAT the
    build appends) as a block device, so UniXNU can read EFI/UniXNU/Payloads/;
  * the uefi-sheng SEC fixes: keep the ABL's x0 (the ABL device tree) in a HOB and clean and
    invalidate the data caches the ABL left behind before this stage's first store;
  * the uefi-sheng BootShim fixes: the ARM64 header describes shim + FV, and the copied FV is
    cleaned to the point of coherency and the I-cache invalidated before the jump.

What is deliberately NOT changed: the Qualcomm panel DisplayDxe (USE_CUSTOM_DISPLAY_DRIVER stays 1,
the OnePlus 6 display driver is a working binary of this port), the SMBIOS strings, the board ID in
the device tree (<8 0 17819 22>, the OnePlus 6 one) and the framebuffer geometry (1080x2280).

Usage: make-enchilada-uefi.py MU_SILICIUM_DIR
"""
import argparse
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]

# The package is patched in place; this line is the marker of a patched checkout.
MARKER = 'enchiladaPkg/Drivers/EnchiladaBootMemoryDxe/EnchiladaBootMemoryDxe.inf'

# (file relative to the package, original, replacement, marker): each edit is applied on its own, so
# a checkout patched by an older version of this script picks up what was added since. `marker` is a
# string that only the replacement introduces; when it is already in the file the edit is done.
# `original` must match exactly once when the edit has not been applied, so an upstream change stops
# the script instead of silently producing a firmware without a boot manager or without XNU loader.
EDITS = [
    ('enchilada.dsc', '  enchilada/AcpiTables.inf\n',
     '  enchilada/AcpiTables.inf\n\n'
     '  #\n  # UniXNU: the boot manager application and the driver that creates its boot option.\n'
     '  #\n'
     '  UniXNUPkg/Application/UniXNU/UniXNU.inf\n'
     '  UniXNUPkg/Driver/UniXNUDxe/UniXNUDxe.inf\n\n'
     '  #\n  # The boot image ramdisk as a block device: the payload FAT volume.\n'
     '  #\n'
     '  enchiladaPkg/Drivers/EnchiladaBootMemoryDxe/EnchiladaBootMemoryDxe.inf\n\n'
     '[LibraryClasses]\n'
     '  UniXNULib|UniXNUPkg/Library/UniXNULib/UniXNULib.inf\n'
     '  FdtLib|MdePkg/Library/BaseFdtLib/BaseFdtLib.inf\n',
     'enchiladaPkg/Drivers/EnchiladaBootMemoryDxe/EnchiladaBootMemoryDxe.inf'),
    # Separate edit, because this line can be missing from a checkout patched before it existed.
    ('enchilada.dsc', '  UniXNUPkg/Driver/UniXNUDxe/UniXNUDxe.inf\n',
     '  UniXNUPkg/Driver/UniXNUDxe/UniXNUDxe.inf\n\n'
     '  #\n  # The XNU loader. It installs gUniXNULoaderProtocolGuid, which is what UniXNU asks BDS\n'
     '  # for before it touches the payload; [payload] loader = "firmware" says it lives here.\n'
     '  #\n'
     '  DarwinBootPkg/Driver/DarwinBoot.inf\n',
     '  DarwinBootPkg/Driver/DarwinBoot.inf\n'),
    ('enchilada.fdf', '  INF SiliciumPkg/Drivers/SmBiosTableDxe/SmBiosTableDxe.inf\n',
     '  INF SiliciumPkg/Drivers/SmBiosTableDxe/SmBiosTableDxe.inf\n\n'
     '  # UniXNU\n'
     '  INF UniXNUPkg/Application/UniXNU/UniXNU.inf\n'
     '  INF UniXNUPkg/Driver/UniXNUDxe/UniXNUDxe.inf\n',
     '  INF UniXNUPkg/Driver/UniXNUDxe/UniXNUDxe.inf\n'),
    ('enchilada.fdf', '  INF UniXNUPkg/Driver/UniXNUDxe/UniXNUDxe.inf\n',
     '  INF UniXNUPkg/Driver/UniXNUDxe/UniXNUDxe.inf\n'
     '  # The XNU loader: it produces gUniXNULoaderProtocolGuid (loader = "firmware").\n'
     '  INF DarwinBootPkg/Driver/DarwinBoot.inf\n',
     '  INF DarwinBootPkg/Driver/DarwinBoot.inf\n'),
    # Loaded where uefi-sheng loads ShengBootMemoryDxe: after the device path protocol exists,
    # because a FAT volume needs it.
    ('Include/DXE.inc', '  INF MdeModulePkg/Universal/DevicePathDxe/DevicePathDxe.inf\n',
     '  INF MdeModulePkg/Universal/DevicePathDxe/DevicePathDxe.inf\n'
     '  INF enchiladaPkg/Drivers/EnchiladaBootMemoryDxe/EnchiladaBootMemoryDxe.inf\n',
     '  INF enchiladaPkg/Drivers/EnchiladaBootMemoryDxe/EnchiladaBootMemoryDxe.inf\n'),
    # clang 18 (and later) tail-merge string literals under -flto -fdata-sections, so the zero padding
    # after an in-line CHAR8 Name[32] inside EFI_MEMORY_REGION_DESCRIPTOR is overwritten by the next
    # string literal. That corrupts the Address/Length fields that follow Name, so the SEC stage cannot
    # locate "Display Reserved" (framebuffer serial log) or "APSS_WDT_TMR1" (watchdog) and the phone
    # falls back to fastboot. Disable constant merging for the firmware volume. Anchored on the
    # FdtLib line the first edit leaves behind, so it survives re-patching in any order.
    # -fno-merge-all-constants must also reach the LTO link (clang acts as the linker), so it goes on
    # both CC_FLAGS and DLINK_FLAGS.
    ('enchilada.dsc', '  FdtLib|MdePkg/Library/BaseFdtLib/BaseFdtLib.inf\n',
     '  FdtLib|MdePkg/Library/BaseFdtLib/BaseFdtLib.inf\n\n'
     '[BuildOptions.common]\n'
     '  # clang 18 tail-merges string-literal tails under -flto/-fdata-sections, which overwrites the\n'
     '  # padding inside an in-line CHAR8 Name[32] and the Address/Length fields after it. Keep each\n'
     '  # constant intact.\n'
     '  CLANGPDB:*_*_*_CC_FLAGS = -fno-merge-all-constants\n'
     '  CLANGPDB:*_*_*_DLINK_FLAGS = -fno-merge-all-constants\n',
     '  CLANGPDB:*_*_*_DLINK_FLAGS = -fno-merge-all-constants\n'),
]

# SEC of SiliciumPkg, shared by every device of the checkout; the uefi-sheng fixes, unchanged for
# this board (they are SoC independent).
SEC = 'Silicon/Silicium/SiliciumPkg'
SEC_MARK = 'gSiliciumEntryArgHobGuid'
SEC_EDITS = [
    ('SiliciumPkg.dec', '[Guids]\n  gSiliciumPkgTokenSpaceGuid          = { 0x055720A6, 0xA739, 0x0047, { 0xBE, 0xC1, 0x67, 0xFC, 0x38, 0x34, 0x54, 0x53 } }\n',
     '[Guids]\n  gSiliciumPkgTokenSpaceGuid          = { 0x055720A6, 0xA739, 0x0047, { 0xBE, 0xC1, 0x67, 0xFC, 0x38, 0x34, 0x54, 0x53 } }\n\n'
     '  # HOB holding the first argument (x0) the previous stage entered SEC with.\n'
     '  # A Linux-style boot loader passes the physical address of a flattened DT.\n'
     '  gSiliciumEntryArgHobGuid            = { 0xDAEE82E6, 0x8090, 0x4E72, { 0xBF, 0xB9, 0x78, 0xC1, 0x3C, 0xD4, 0xE3, 0x59 } }\n'),
    ('Sec/Sec.inf', '[Guids]\n  gArmMpCoreInfoGuid\n', '[Guids]\n  gArmMpCoreInfoGuid\n  gSiliciumEntryArgHobGuid\n'),
    ('Sec/Sec.c', '#include "Sec.h"\n',
     '#include "Sec.h"\n\n'
     '// First argument (x0) of the previous stage, saved by ModuleEntryPoint.S\n'
     'extern UINT64  gSecEntryArg0;\n\n'
     'VOID\nSecCleanInvalidateDataCacheAll (\n  VOID\n  );\n'),
    ('Sec/Sec.c', '  // Build CPU HOB\n',
     '  // Pass on what the previous stage handed over in x0 (a DT address for a\n'
     '  // Linux-style boot loader), so a DXE driver can use it.\n'
     '  {\n    UINT64  EntryArg = gSecEntryArg0;\n\n'
     '    BuildGuidDataHob (&gSiliciumEntryArgHobGuid, &EntryArg, sizeof (EntryArg));\n  }\n\n'
     '  // Build CPU HOB\n'),
    ('Sec/Sec.c', 'SecEntry (VOID)\n{\n  // Disable Data Cache\n',
     'SecEntry (VOID)\n{\n'
     '  // The previous stage ran with its caches on. Clean and invalidate them before anything else: the UEFI writes its HOB tables and heap with\n'
     '  // the MMU and caches off, and lines the previous stage left would hide those writes (or be written back over them) once the caches are on.\n'
     '  SecCleanInvalidateDataCacheAll ();\n\n'
     '  // Disable Data Cache\n'),
    ('Sec/AArch64/ModuleEntryPoint.S', 'ASM_FUNC (_ModuleEntryPoint)\n  // Do Platform Specific Initialization\n',
     'ASM_FUNC (_ModuleEntryPoint)\n'
     '  // Before the first store of this stage: clean and invalidate the data caches the previous stage filled (x0 is kept in x20).\n'
     '  mov   x20, x0\n  bl    ASM_PFX (SecCleanInvalidateDataCacheAll)\n  mov   x0, x20\n\n'
     '  // Keep x0 for the platform. It is the address of the DT a Linux-style boot\n'
     '  // loader hands over. The slot is in this section, which is in RAM here.\n'
     '  adr   x1, ASM_PFX (gSecEntryArg0)\n  str   x0, [x1]\n\n'
     '  // Do Platform Specific Initialization\n'),
]
SEC_APPEND = {
    'Sec/AArch64/ModuleEntryPoint.S': '''
  // First argument of the previous stage, written once at entry
  .align 3
  .global ASM_PFX (gSecEntryArg0)
ASM_PFX (gSecEntryArg0):
  .quad 0
''',
}
SEC_HELPER = '''//
// Cleans and invalidates every data cache level up to the Level of Coherence by set/way (the standard sequence: CLIDR_EL1 levels,
// CSSELR_EL1 to select, CCSIDR_EL1 geometry, DC CISW). The previous stage ran with its caches on and leaves lines behind that the UEFI,
// running with the MMU and caches off until ArmConfigureMmu, never wrote: once the caches are on, those stale lines would hide the
// DRAM contents (HOB tables, heap) written in between, or be written back over them. Cleaning keeps what the previous stage wrote.
// Corrupts x0-x11; callable with the MMU on or off.
//
ASM_FUNC (SecCleanInvalidateDataCacheAll)
  dmb   sy
  mrs   x0, clidr_el1
  and   x3, x0, #0x7000000          // LoC, shifted
  lsr   x3, x3, #23                 // LoC * 2
  cbz   x3, 5f
  mrs   x1, id_aa64mmfr2_el1
  ubfx  x8, x1, #20, #4             // CCIDX
  mov   x10, #0                     // level * 2
1:
  add   x2, x10, x10, lsr #1
  lsr   x1, x0, x2
  and   x1, x1, #7                  // cache type of this level
  cmp   x1, #2
  b.lt  4f                          // no data cache here
  msr   csselr_el1, x10
  isb
  mrs   x1, ccsidr_el1
  and   x2, x1, #7
  add   x2, x2, #4                  // log2 (line bytes)
  cbz   x8, 6f
  ubfx  x4, x1, #3, #21             // CCIDX: associativity - 1
  ubfx  x7, x1, #32, #24            // CCIDX: sets - 1
  b     7f
6:
  ubfx  x4, x1, #3, #10             // associativity - 1
  ubfx  x7, x1, #13, #15            // sets - 1
7:
  clz   w5, w4                      // bit position of the way field
2:
  mov   x9, x4
3:
  lsl   x6, x9, x5
  orr   x11, x10, x6                // way and level
  lsl   x6, x7, x2
  orr   x11, x11, x6                // set
  dc    cisw, x11
  subs  x9, x9, #1
  b.ge  3b
  subs  x7, x7, #1
  b.ge  2b
4:
  add   x10, x10, #2
  cmp   x3, x10
  b.gt  1b
5:
  mov   x10, #0
  msr   csselr_el1, x10
  dsb   sy
  isb
  ret

'''

# BootShim, shared by every device of the checkout; the ARM64 header describes the shim plus the
# firmware volume, which unixnu bootimg checks, and the copied firmware volume is cleaned to the
# point of coherency and the instruction cache invalidated before the jump into it.
SHIM = 'BootShim/BootShim.S'
SHIM_MARK = '/* Complete shim and payload */'
SHIM_EDITS = [
    ('  .quad FD_SIZE         /* Image Size */', f'  .quad FD_SIZE + (_Payload - _Head) {SHIM_MARK}'),
    ('  b.ne  _CopyLoop\n  ldr   x5, =FD_BASE\n',
     '  b.ne  _CopyLoop\n  ldr   x5, =FD_BASE\n\n'
     '  /* Publish the copied instructions before executing them. */\n'
     '  dsb   sy\n  mrs   x9, ctr_el0\n  ubfx  x9, x9, #16, #4\n  mov   x10, #4\n  lsl   x9, x10, x9\n'
     '  sub   x10, x9, #1\n  bic   x8, x5, x10\n  ldr   x7, =FD_SIZE\n  add   x7, x5, x7\n\n'
     '_CleanLoop:\n  dc    cvac, x8\n  add   x8, x8, x9\n  cmp   x8, x7\n  b.lo  _CleanLoop\n'
     '  dsb   sy\n  ic    iallu\n  dsb   sy\n  isb\n'),
    ('  b     _Dead\n', '  b     _Dead\n.ltorg\n'),
]


def read(path):
    """The text of a file and its line ending; files keep the line ending they have (several of the checkout use CRLF).

    Read and write the bytes directly: Path.read_text(newline=...) only exists from Python 3.13 on, and the script is
    also run by hand on older interpreters.
    """
    text = path.read_bytes().decode('utf-8')
    return text, '\r\n' if '\r\n' in text else '\n'


def replace_once(path, original, replacement):
    text, eol = read(path)
    original, replacement = original.replace('\n', eol), replacement.replace('\n', eol)
    count = text.count(original)
    if count != 1:
        raise SystemExit(f'{path}: expected one match of {original!r}, found {count}')
    path.write_bytes(text.replace(original, replacement).encode('utf-8'))


def append(path, addition):
    text, eol = read(path)
    path.write_bytes((text + addition.replace('\n', eol)).encode('utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('uefi', type=Path, help='Mu-Silicium checkout')
    args = parser.parse_args()

    uefi = args.uefi.resolve()
    target = uefi / 'Platforms/OnePlus/enchiladaPkg'
    if not (target / 'enchilada.dsc').is_file():
        raise SystemExit(f'not a Mu-Silicium checkout with the OnePlus 6 port: {target}')

    if MARKER not in read(target / 'enchilada.dsc')[0]:
        memory = target / 'Drivers/EnchiladaBootMemoryDxe'
        if memory.exists():
            shutil.rmtree(memory)
        shutil.copytree(ROOT / 'uefi/EnchiladaBootMemoryDxe', memory)
    # Every edit is applied on its own: a checkout patched before an edit existed picks that edit up,
    # and a checkout patched after it does not see it twice.
    added = 0
    for name, original, replacement, marker in EDITS:
        path = target / name
        if marker in read(path)[0]:
            continue
        replace_once(path, original, replacement)
        added += 1
    if added:
        print(f'port: {target} patched ({added} edit(s) applied)')
    else:
        print(f'port: {target} already patched')

    sec = uefi / SEC
    if SEC_MARK in read(sec / 'SiliciumPkg.dec')[0]:
        print(f'SEC: {sec} already patched')
    else:
        for name, original, replacement in SEC_EDITS:
            replace_once(sec / name, original, replacement)
        for name, text in SEC_APPEND.items():
            append(sec / name, text)
        replace_once(sec / 'Sec/AArch64/Helper.S', 'ASM_FUNCTION_REMOVE_IF_UNREFERENCED', SEC_HELPER + 'ASM_FUNCTION_REMOVE_IF_UNREFERENCED')
        print(f'SEC: {sec} patched')

    shim = uefi / SHIM
    if SHIM_MARK in read(shim)[0]:
        print(f'boot shim: {shim} already patched')
    else:
        for original, replacement in SHIM_EDITS:
            replace_once(shim, original, replacement)
        print(f'boot shim: {shim} patched')

    # The device tree the boot image carries: the official OnePlus 6 one, compiled. Its board ID is
    # already the OnePlus 6 one (<8 0 17819 22>), so no edit is needed; only the .dtb has to be built.
    dts = uefi / 'Resources/DTBs/enchilada.dts'
    dtb = dts.with_suffix('.dtb')
    if shutil.which('dtc'):
        subprocess.run(['dtc', '-q', '-I', 'dts', '-O', 'dtb', '-o', str(dtb), str(dts)], check=True)
        print(f'device tree: {dtb}')
    else:
        print(f'dtc not found (dnf install dtc, or brew install dtc); before unixnu bootimg, run:\n'
              f'  dtc -I dts -O dtb -o {dtb} {dts}')

    print(f'firmware port ready: {target}')


if __name__ == '__main__':
    main()
