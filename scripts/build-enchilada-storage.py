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
    # The storage audit is static against the *original* IOStorageFamily module, which is present byte-for-byte in the
    # pinned kernelcache, so audit the kernelcache itself (with the [kernel.prelink] slide). Auditing a prelinked
    # candidate instead is fragile: emit_prelink rebinds the inherited-IOService dispatch pointers to the native
    # kernel's symbol table, and any defconfig difference (e.g. the bootstrap pass) shifts those addresses so the
    # audit's `named` count no longer matches and it reports "0 candidates". The kernelcache never has that problem.
    #
    # The candidate is still needed to *stage* the native-owner-ufs-rootauth3-candidate.macho that
    # build-sheng-platform-power.py and build-native-legacy-display.py read; those audits only check a handful of
    # IOKit core symbols (IOService::serviceMatching/safeMetaCast/newUserClient, ___cxa_pure_virtual) whose addresses
    # are defconfig-independent, so the bootstrap candidate qualifies for them. Decouple the two roles below.
    original = ROOT.parents[1] / 'ipsw/21H461/kernelcache.decompressed'
    stage_candidate = os.environ.get('ENCHILADA_STORAGE_CANDIDATE')
    if not stage_candidate:
        previous = ROOT.parent / 'kernel/kernel.macho'
        if previous.is_file():
            stage_candidate = str(previous)

    # Audit input: always the kernelcache when it is fetched (cold start and full pass alike).
    audit_candidate = original if original.is_file() else None
    audit = os.environ.get('ENCHILADA_STORAGE_AUDIT')
    if audit and not audit_candidate:
        # No kernelcache, but a pre-made audit was supplied for an earlier candidate; use that candidate.
        audit_candidate = stage_candidate or ROOT.parent / 'kernel/kernel.macho'
    if not audit and not audit_candidate:
        raise SystemExit('the pinned kernelcache must be fetched ("unixnu ipsw fetch KernelCache") '
                         'to audit the original storage interface')

    # Stage the candidate that sheng/display read, independent of how the audit is fed.
    prelink = ROOT / 'build/native-prelink'
    print(f'stage_candidate={stage_candidate!r} exists={Path(stage_candidate).is_file() if stage_candidate else False}', flush=True)
    if stage_candidate and Path(stage_candidate).is_file():
        prelink.mkdir(parents=True, exist_ok=True)
        for suffix in ('.macho', '.json'):
            source = Path(stage_candidate).with_suffix(suffix)
            if source.is_file():
                shutil.copyfile(source, prelink / f'native-owner-ufs-rootauth3-candidate{suffix}')
                print(f'staged {source} -> {prelink / f"native-owner-ufs-rootauth3-candidate{suffix}"}', flush=True)
            else:
                print(f'candidate sidecar missing: {source}', flush=True)
    else:
        print('no candidate to stage (ENCHILADA_STORAGE_CANDIDATE unset or missing)', flush=True)

    if original.is_file():
        staged = ROOT / '.cache/firmware/kernelcache.macho/kernelcache.release.ipad7.decompressed'
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, staged)
    if audit:
        recorded = json.loads(Path(audit).read_text())['candidate_sha256']
        if recorded != hashlib.sha256(Path(audit_candidate).read_bytes()).hexdigest():
            raise SystemExit(f'{audit} was made for candidate {recorded}, not {audit_candidate}')
        (ROOT / 'build/storage-abi').mkdir(parents=True, exist_ok=True)
        shutil.copyfile(audit, ROOT / 'build/storage-abi/interface-audit.json')
        print(f'storage audit: {audit} (candidate {recorded[:16]})', flush=True)
    else:
        command = [ROOT / 'scripts/audit-storage-interface-static.py', '--prelinked', str(audit_candidate)]
        if audit_candidate == original:
            # The original kernelcache is audited at the board's fixed prelink slide. A negative slide
            # (--slide -0x1000000) is misread by argparse as an option; pass it in the --slide=VALUE form
            # so the leading minus survives as the argument value.
            command += ['--slide=-0x1000000']
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
