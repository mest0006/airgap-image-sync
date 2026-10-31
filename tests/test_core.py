import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from airgap_sync import cli, core  # noqa: E402

D1 = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64


class ParseRefTest(unittest.TestCase):
    def test_bare_name_gets_docker_library(self):
        self.assertEqual(core.parse_ref("nginx:1.27"), ("docker.io/library/nginx", "1.27", None))

    def test_user_repo_gets_docker_io(self):
        self.assertEqual(core.parse_ref("grafana/grafana:11.2.0")[0], "docker.io/grafana/grafana")

    def test_registry_with_port_kept(self):
        self.assertEqual(core.parse_ref("registry.local:5000/team/app:1.0")[0], "registry.local:5000/team/app")

    def test_digest_only_allowed(self):
        self.assertEqual(core.parse_ref(f"ghcr.io/org/app@{D1}"), ("ghcr.io/org/app", None, D1))

    def test_latest_rejected(self):
        with self.assertRaises(core.ImageRefError):
            core.parse_ref("nginx:latest")

    def test_untagged_rejected(self):
        with self.assertRaises(core.ImageRefError):
            core.parse_ref("quay.io/prometheus/prometheus")

    def test_latest_with_digest_allowed(self):
        # The digest pins it, so the tag is just a label.
        self.assertEqual(core.parse_ref(f"nginx:latest@{D1}")[2], D1)

    def test_garbage_rejected(self):
        with self.assertRaises(core.ImageRefError):
            core.parse_ref("Not An Image!")


class ResolveAndPlanTest(unittest.TestCase):
    def setUp(self):
        images = [core.parse_ref("nginx:1.27"), core.parse_ref(f"ghcr.io/org/app:2.0@{D2}")]
        self.lock = core.resolve(images, digest_lookup=lambda repo, tag: D1)

    def test_resolve_fills_digests_and_sorts(self):
        self.assertEqual([e["repository"] for e in self.lock], ["docker.io/library/nginx", "ghcr.io/org/app"])
        self.assertEqual(self.lock[0]["digest"], D1)
        self.assertEqual(self.lock[1]["digest"], D2)  # existing digest is kept

    def test_export_copies_by_digest(self):
        cmds = core.plan_export(self.lock, "/media")
        self.assertIn(f"docker://docker.io/library/nginx@{D1}", cmds[0])
        self.assertIn("--preserve-digests", cmds[0])

    def test_import_rewrites_to_internal_registry(self):
        cmds = core.plan_import(self.lock, "/media", "registry.internal:5000")
        self.assertEqual(cmds[0][-1], "docker://registry.internal:5000/library/nginx:1.27")
        self.assertIn("--preserve-digests", cmds[0])

    def test_deploy_refs_are_by_digest(self):
        refs = core.deploy_refs(self.lock, "registry.internal:5000")
        self.assertEqual(refs, [f"registry.internal:5000/library/nginx@{D1}", f"registry.internal:5000/org/app@{D2}"])

    def test_skopeo_digest_validates_output(self):
        class Fake:
            stdout = "not-a-digest\n"
        with self.assertRaises(RuntimeError):
            core.skopeo_digest("docker.io/library/nginx", "1.27", runner=lambda *a, **k: Fake())


class ManifestTest(unittest.TestCase):
    def test_verify_detects_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "img"))
            with open(os.path.join(d, "img", "blob"), "wb") as f:
                f.write(b"layer-bytes")
            manifest = core.build_manifest(d)
            self.assertEqual(core.verify_manifest(d, manifest), [])
            with open(os.path.join(d, "img", "blob"), "ab") as f:
                f.write(b"x")
            with open(os.path.join(d, "extra"), "w") as f:
                f.write("?")
            problems = core.verify_manifest(d, manifest)
            self.assertIn("checksum mismatch: img/blob", problems)
            self.assertIn("unexpected: extra", problems)


class CliTest(unittest.TestCase):
    def test_bad_image_list_exit_code(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# images\nnginx:latest\n")
        try:
            self.assertEqual(cli.main(["resolve", f.name, "-o", os.devnull]), 2)
        finally:
            os.unlink(f.name)

    def test_manifest_and_verify_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a"), "w") as f:
                f.write("1")
            self.assertEqual(cli.main(["manifest", d]), 0)
            self.assertEqual(cli.main(["verify", d]), 0)
            with open(os.path.join(d, "a"), "w") as f:
                f.write("2")
            self.assertEqual(cli.main(["verify", d]), 1)


if __name__ == "__main__":
    unittest.main()
