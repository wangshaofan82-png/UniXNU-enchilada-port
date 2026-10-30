#!/usr/bin/env python3
"""Storage inputs of the enchilada kernel: the storage ABI audit and the NativeVirtioBlock bindings NativeUfs links against.

Runs as a [kernel] archives builder, after the bootstrap pass left the compile records. The audit is static
(audit-storage-interface-static.py --prelinked) against a prelinked kernel of this board: by default the kernel step's
own output of an earlier pass (out/<device>/kernel/kernel.macho, with its kernel.json prelink report beside it), or
ENCHILADA_STORAGE_CANDIDATE naming one explicitly. The bound addresses are those of the original IOStorageFamily at the
board-independent slide, so any prelink of the same kernelcache qualifies. Copies of the audit and the bindings report
go to the logs directory, which the macOS workflow uploads.

ENCHILADA_STORAGE_AUDIT may name an interface-audit.json already made for the same candidate (for example by the
macOS workflow): it is used after its candidate hash is checked, instead of auditing the image again.

It also stages the inputs of build-sheng-platform-power.py, which runs after it: the same candidate under the name
that script reads, and the decompressed original kernelcache (fetch it first with "unixnu ipsw fetch KernelCache").
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT.parent / 'logs'


def run(*command):
    print('+', ' '.join(str(part) for part in command), flush=True)
    subprocess.run(['python3', *command], cwd=ROOT, check=True)


def main():
    candidate = os.environ.get('ENCHILADA_STORAGE_CANDIDATE')
    slide = 0
    from_kernelcache = False
    if not candidate:
        # A prelink of an earlier pass over the same original kernelcache: the bound addresses are the original
        # IOStorageFamily's at the board-independent slide, so any prelink of that kernelcache qualifies.
        previous = ROOT.parent / 'kernel/kernel.macho'
        if previous.is_file():
            candidate = str(previous)
    if not candidate or not Path(candidate).is_file():
        # Cold start: there is no earlier prelink to audit. The audit is static against the original IOStorageFamily
        # module, which is present byte-for-byte in the pinned kernelcache, so audit the kernelcache itself with the
        # [kernel.prelink] slide. This is exactly what makes a first build from scratch possible (see the docstring of
        # audit-storage-interface-static.py). The kernelcache has no kernel.json prelink report beside it, so the
        # staging of native-owner-ufs-rootauth3-candidate is skipped this pass.
        original = ROOT.parents[1] / 'ipsw/21H461/kernelcache.decompressed'
        if original.is_file():
            candidate = str(original)
            slide = -0x1000000
            from_kernelcache = True
    if not candidate or not Path(candidate).is_file():
        raise SystemExit('ENCHILADA_STORAGE_CANDIDATE must name a prelinked enchilada kernel.macho with its kernel.json '
                         'beside it, or the pinned kernelcache must be fetched ("unixnu ipsw fetch KernelCache")')
    audit = os.environ.get('ENCHILADA_STORAGE_AUDIT')
    if not audit and not from_kernelcache and not Path(candidate).with_suffix('.json').is_file():
        raise SystemExit(f'{candidate} has no prelink report beside it; supply ENCHILADA_STORAGE_AUDIT for it')
    prelink = ROOT / 'build/native-prelink'
    if not from_kernelcache:
        prelink.mkdir(parents=True, exist_ok=True)
        for suffix in ('.macho', '.json'):
            source = Path(candidate).with_suffix(suffix)
            if source.is_file():
                shutil.copyfile(source, prelink / f'native-owner-ufs-rootauth3-candidate{suffix}')
    original = ROOT.parents[1] / 'ipsw/21H461/kernelcache.decompressed'
    if original.is_file():
        staged = ROOT / '.cache/firmware/kernelcache.macho/kernelcache.release.ipad7.decompressed'
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, staged)
    if audit:
        recorded = json.loads(Path(audit).read_text())['candidate_sha256']
        if recorded != hashlib.sha256(Path(candidate).read_bytes()).hexdigest():
            raise SystemExit(f'{audit} was made for candidate {recorded}, not {candidate}')
        (ROOT / 'build/storage-abi').mkdir(parents=True, exist_ok=True)
        shutil.copyfile(audit, ROOT / 'build/storage-abi/interface-audit.json')
        print(f'storage audit: {audit} (candidate {recorded[:16]})', flush=True)
    else:
        command = [ROOT / 'scripts/audit-storage-interface-static.py', '--prelinked', candidate]
        if slide:
            command += ['--slide', hex(slide)]
        run(*command)
    run(ROOT / 'scripts/build-arm-driver.py', 'NativeVirtioBlock', '--verified-root', '--verified-root-bundle')
    run(ROOT / 'scripts/build-native-virtio-bindings.py', '--component', 'NativeVirtioBlock',
        '--directory', 'build/native-virtio-block-verified-bundle')
    LOGS.mkdir(parents=True, exist_ok=True)
    for source in (ROOT / 'build/storage-abi/interface-audit.json',
                   ROOT / 'build/native-virtio-block-verified-bundle/bindings.json'):
        if source.is_file():
            shutil.copyfile(source, LOGS / f'storage-{source.name}')


if __name__ == '__main__':
    main()
