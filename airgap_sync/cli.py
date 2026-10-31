"""Command-line entry point: python -m airgap_sync <command> ..."""

import argparse
import json
import os
import subprocess
import sys

from . import core


def main(argv=None):
    p = argparse.ArgumentParser(prog="airgap-sync", description=core.__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("resolve", help="image list -> digest lockfile (connected side)")
    r.add_argument("images")
    r.add_argument("-o", "--output", default="images.lock.json")

    e = sub.add_parser("plan-export", help="print skopeo commands to copy images onto media")
    e.add_argument("lockfile")
    e.add_argument("--media", required=True)

    m = sub.add_parser("manifest", help="write MANIFEST.json for the media directory")
    m.add_argument("media")

    v = sub.add_parser("verify", help="check media against MANIFEST.json (air-gapped side)")
    v.add_argument("media")

    i = sub.add_parser("plan-import", help="print skopeo commands to load media into the internal registry")
    i.add_argument("lockfile")
    i.add_argument("--media", required=True)
    i.add_argument("--registry", required=True, help="internal registry, e.g. registry.internal:5000")

    a = p.parse_args(argv)
    try:
        if a.cmd == "resolve":
            lock = core.resolve(core.read_image_list(a.images))
            core.dump_json(lock, a.output)
            print(f"wrote {len(lock)} images to {a.output}")
        elif a.cmd == "plan-export":
            print(core.render(core.plan_export(json.load(open(a.lockfile)), a.media)))
        elif a.cmd == "manifest":
            manifest = core.build_manifest(a.media)
            core.dump_json(manifest, os.path.join(a.media, "MANIFEST.json"))
            print(f"hashed {len(manifest['files'])} files")
        elif a.cmd == "verify":
            manifest = json.load(open(os.path.join(a.media, "MANIFEST.json")))
            problems = core.verify_manifest(a.media, manifest)
            if problems:
                print("\n".join(problems), file=sys.stderr)
                return 1
            print(f"verified {len(manifest['files'])} files")
        elif a.cmd == "plan-import":
            lock = json.load(open(a.lockfile))
            print(core.render(core.plan_import(lock, a.media, a.registry)))
            print("# deploy these by-digest references inside the enclave:")
            for ref in core.deploy_refs(lock, a.registry):
                print(f"#   {ref}")
    except core.ImageRefError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    except FileNotFoundError as err:
        print(f"error: {err.filename or err} not found (is skopeo installed?)", file=sys.stderr)
        return 3
    except (RuntimeError, subprocess.CalledProcessError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
