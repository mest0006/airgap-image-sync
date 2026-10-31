"""Core logic: parse an image list, resolve it to a digest lockfile, plan the
transfer through the DMZ, and verify what arrived on the inside.

Workflow (each step is a separate command so it can run on a different host):

  1. resolve   (connected side) image list -> lockfile of name + tag + digest
  2. plan      (connected side) lockfile -> skopeo commands that copy each image
               by digest into an OCI archive directory on transfer media
  3. manifest  (connected side) write a SHA-256 manifest of every archive file
  4. verify    (air-gapped side) re-hash the media and compare to the manifest
  5. push-plan (air-gapped side) skopeo commands that load archives into the
               internal registry, rewritten to the internal hostname, by digest

Images are always referenced by digest after step 1, so the cluster runs
exactly the bytes that were scanned, and a mutable tag like ":latest" can
never drift between the two sides.
"""

import hashlib
import json
import os
import re
import shlex
import subprocess

IMAGE_RE = re.compile(
    r"^(?P<repo>[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._/-][a-z0-9]+)*)+|[a-z0-9]+(?:[._-][a-z0-9]+)*)"
    r"(?::(?P<tag>[\w][\w.-]{0,127}))?(?:@(?P<digest>sha256:[a-f0-9]{64}))?$"
)


class ImageRefError(ValueError):
    pass


def parse_ref(ref):
    """Split an image reference into (repository, tag, digest).

    Bare names get the docker.io/library/ prefix. ":latest" and untagged
    references without a digest are rejected: they are not reproducible.
    """
    ref = ref.strip()
    m = IMAGE_RE.match(ref)
    if not m:
        raise ImageRefError(f"invalid image reference: {ref!r}")
    repo, tag, digest = m.group("repo"), m.group("tag"), m.group("digest")
    first = repo.split("/", 1)[0]
    if "/" not in repo:
        repo = f"docker.io/library/{repo}"
    elif "." not in first and ":" not in first and first != "localhost":
        repo = f"docker.io/{repo}"
    if not digest and (tag is None or tag == "latest"):
        raise ImageRefError(f"{ref!r}: pin a version tag or a digest (':latest' and untagged images are not allowed)")
    return repo, tag, digest


def read_image_list(path):
    """Read one image per line; blank lines and '#' comments are ignored."""
    images = []
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            try:
                images.append(parse_ref(line))
            except ImageRefError as e:
                raise ImageRefError(f"{path}:{n}: {e}") from None
    return images


def skopeo_digest(repo, tag, runner=subprocess.run):
    """Ask the registry for the digest of repo:tag (connected side only)."""
    out = runner(
        ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{repo}:{tag}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", out):
        raise RuntimeError(f"unexpected digest from skopeo for {repo}:{tag}: {out!r}")
    return out


def resolve(images, digest_lookup=skopeo_digest):
    """Turn parsed images into lockfile entries, resolving tags to digests."""
    lock = []
    for repo, tag, digest in images:
        if not digest:
            digest = digest_lookup(repo, tag)
        lock.append({"repository": repo, "tag": tag, "digest": digest})
    return sorted(lock, key=lambda e: (e["repository"], e["tag"] or ""))


def archive_name(entry):
    """Stable, filesystem-safe archive directory name for an image."""
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", entry["repository"]).strip("_")
    return f"{safe}__{entry['digest'].split(':')[1][:16]}"


def plan_export(lock, media_dir):
    """skopeo commands that copy each image by digest to an OCI directory."""
    cmds = []
    for e in lock:
        src = f"docker://{e['repository']}@{e['digest']}"
        dst = f"oci:{os.path.join(media_dir, archive_name(e))}:{e['tag'] or 'pinned'}"
        cmds.append(["skopeo", "copy", "--all", "--preserve-digests", src, dst])
    return cmds


def internal_ref(entry, internal_registry):
    """Rewrite an upstream repository onto the internal registry."""
    _, _, path = entry["repository"].partition("/")
    return f"{internal_registry.rstrip('/')}/{path}"


def internal_tag(entry):
    """Tag used inside: the upstream tag, or a digest-derived tag if there was none."""
    return entry["tag"] or "pinned-" + entry["digest"].split(":")[1][:12]


def plan_import(lock, media_dir, internal_registry):
    """skopeo commands that push each archive into the internal registry.

    --preserve-digests keeps the manifest bytes identical, so the image is
    still addressable inside by the same digest recorded in the lockfile.
    Workloads should deploy <internal repo>@<digest>, never the tag.
    """
    cmds = []
    for e in lock:
        src = f"oci:{os.path.join(media_dir, archive_name(e))}:{e['tag'] or 'pinned'}"
        dst = f"docker://{internal_ref(e, internal_registry)}:{internal_tag(e)}"
        cmds.append(["skopeo", "copy", "--all", "--preserve-digests", src, dst])
    return cmds


def deploy_refs(lock, internal_registry):
    """The by-digest references workloads should use inside the enclave."""
    return [f"{internal_ref(e, internal_registry)}@{e['digest']}" for e in lock]


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def build_manifest(media_dir):
    """SHA-256 of every file under media_dir, keyed by relative path."""
    files = {}
    for root, _, names in os.walk(media_dir):
        for name in sorted(names):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, media_dir)
            if rel == "MANIFEST.json":
                continue
            files[rel] = sha256_file(full)
    return {"version": 1, "files": dict(sorted(files.items()))}


def verify_manifest(media_dir, manifest):
    """Compare media contents to the manifest. Returns a list of problems."""
    actual = build_manifest(media_dir)["files"]
    expected = manifest["files"]
    problems = []
    for rel in sorted(set(expected) - set(actual)):
        problems.append(f"missing: {rel}")
    for rel in sorted(set(actual) - set(expected)):
        problems.append(f"unexpected: {rel}")
    for rel in sorted(set(expected) & set(actual)):
        if expected[rel] != actual[rel]:
            problems.append(f"checksum mismatch: {rel}")
    return problems


def render(cmds):
    return "\n".join(shlex.join(c) for c in cmds)


def dump_json(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
