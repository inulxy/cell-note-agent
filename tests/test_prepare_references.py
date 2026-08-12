"""prepare_references plan/fetch/verify: pinned URLs + sha256, no silent success.

Network is mocked; payload checksums are computed in-test and patched into the
module's asset table, so these tests validate the verification logic itself.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from script_test_utils import import_script

pr = import_script("prepare_references")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


CHROM_SIZES_BYTES = b"chr1\t248956422\nchr2\t242193529\nchrX\t156040895\n"
BLACKLIST_BED_BYTES = (
    b"chr1\t100\t200\tHigh Signal Region\nchr2\t5\t50\tLow Mappability\n"
)
BLACKLIST_GZ_BYTES = gzip.compress(BLACKLIST_BED_BYTES)
CHAIN_BYTES = b"chain 1 chr1 100 + 0 100 chr1 100 + 0 100 1\n"

URLS = {
    "chrom_sizes": "https://example.test/hg38.chrom.sizes",
    "blacklist": "https://example.test/hg38-blacklist.v2.bed.gz",
    "liftover_chain": "https://example.test/hg19ToHg38.over.chain.gz",
}
PAYLOADS = {
    URLS["chrom_sizes"]: CHROM_SIZES_BYTES,
    URLS["blacklist"]: BLACKLIST_GZ_BYTES,
    URLS["liftover_chain"]: CHAIN_BYTES,
}


def fake_assets() -> dict:
    return {
        "chrom_sizes": {
            "filename": "hg38.chrom.sizes",
            "url": URLS["chrom_sizes"],
            "sha256": _sha(CHROM_SIZES_BYTES),
            "liftover": False,
        },
        "blacklist": {
            "filename": "hg38-blacklist.v2.bed.gz",
            "url": URLS["blacklist"],
            "sha256": _sha(BLACKLIST_GZ_BYTES),
            "gunzip_to": "hg38-blacklist.v2.bed",
            "liftover": False,
        },
        "liftover_chain": {
            "filename": "hg19ToHg38.over.chain.gz",
            "url": URLS["liftover_chain"],
            "sha256": _sha(CHAIN_BYTES),
            "liftover": True,
        },
    }


def fake_download(url: str, dest: str) -> None:
    Path(dest).write_bytes(PAYLOADS[url])


def no_network(url: str, dest: str) -> None:
    raise AssertionError(f"unexpected download attempt: {url}")


def make_args(out: str, include_liftover: bool = False, genome_build: str = "GRCh38"):
    return Namespace(out=out, genome_build=genome_build, include_liftover=include_liftover)


class PatchedCase(unittest.TestCase):
    """Base: temp out dir + patched asset table."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = self.tmp.name
        patcher = mock.patch.object(pr, "ASSETS", fake_assets())
        patcher.start()
        self.addCleanup(patcher.stop)

    def fetch_ok(self, include_liftover: bool = False):
        with mock.patch.object(pr, "_download", fake_download):
            pr.fetch(make_args(self.out, include_liftover=include_liftover))


class PlanTests(PatchedCase):
    def test_plan_writes_plan_without_network(self):
        with mock.patch.object(pr, "_download", no_network):
            pr.plan(make_args(self.out))
        plan = json.loads((Path(self.out) / "reference_plan.json").read_text())
        names = {a["name"] for a in plan["assets"]}
        self.assertEqual(names, {"chrom_sizes", "blacklist"})
        for asset in plan["assets"]:
            self.assertIn("url", asset)
            self.assertIn("sha256", asset)
        self.assertEqual(plan["genome_build"], "GRCh38")

    def test_plan_include_liftover_adds_chain(self):
        pr.plan(make_args(self.out, include_liftover=True))
        plan = json.loads((Path(self.out) / "reference_plan.json").read_text())
        names = {a["name"] for a in plan["assets"]}
        self.assertIn("liftover_chain", names)

    def test_non_grch38_rejected(self):
        with self.assertRaises(SystemExit):
            pr.plan(make_args(self.out, genome_build="hg19"))


class FetchTests(PatchedCase):
    def test_fetch_downloads_verifies_and_gunzips(self):
        self.fetch_ok()
        out = Path(self.out)
        self.assertEqual(
            (out / "hg38.chrom.sizes").read_bytes(), CHROM_SIZES_BYTES
        )
        self.assertEqual(
            (out / "hg38-blacklist.v2.bed").read_bytes(), BLACKLIST_BED_BYTES
        )
        self.assertFalse(list(out.glob("*.tmp")), "tmp files must not remain")
        # default excludes liftover chain
        self.assertFalse((out / "hg19ToHg38.over.chain.gz").exists())

    def test_fetch_include_liftover(self):
        self.fetch_ok(include_liftover=True)
        self.assertEqual(
            (Path(self.out) / "hg19ToHg38.over.chain.gz").read_bytes(), CHAIN_BYTES
        )

    def test_fetch_rejects_checksum_mismatch(self):
        def bad_download(url, dest):
            Path(dest).write_bytes(b"corrupted payload")

        with mock.patch.object(pr, "_download", bad_download):
            with self.assertRaises(SystemExit):
                pr.fetch(make_args(self.out))
        self.assertFalse((Path(self.out) / "hg38.chrom.sizes").exists())

    def test_fetch_skips_existing_verified_files(self):
        self.fetch_ok()
        # second run must not touch the network at all
        with mock.patch.object(pr, "_download", no_network):
            pr.fetch(make_args(self.out))


class VerifyTests(PatchedCase):
    def test_verify_passes_and_writes_manifest(self):
        self.fetch_ok()
        pr.verify(make_args(self.out))
        manifest = json.loads(
            (Path(self.out) / "reference_manifest.json").read_text()
        )
        self.assertEqual(manifest["genome_build"], "GRCh38")
        assets = manifest["assets"]
        self.assertIn("chrom_sizes", assets)
        self.assertIn("blacklist", assets)
        for entry in assets.values():
            self.assertIn("url", entry)
            self.assertIn("sha256", entry)
            self.assertGreater(entry["bytes"], 0)

    def test_verify_fails_on_missing_asset(self):
        self.fetch_ok()
        (Path(self.out) / "hg38.chrom.sizes").unlink()
        with self.assertRaises(SystemExit):
            pr.verify(make_args(self.out))
        self.assertFalse((Path(self.out) / "reference_manifest.json").exists())

    def test_verify_fails_on_corrupted_asset(self):
        self.fetch_ok()
        (Path(self.out) / "hg38.chrom.sizes").write_bytes(b"chr1\tnot_an_int\n")
        with self.assertRaises(SystemExit):
            pr.verify(make_args(self.out))

    def test_verify_rejects_malformed_chrom_sizes_even_if_sha_matches(self):
        self.fetch_ok()
        bad = b"chr1\tnot_an_int\n"
        (Path(self.out) / "hg38.chrom.sizes").write_bytes(bad)
        pr.ASSETS["chrom_sizes"]["sha256"] = _sha(bad)
        with self.assertRaises(SystemExit):
            pr.verify(make_args(self.out))


class RealAssetTableTests(unittest.TestCase):
    """The real (unpatched) asset table must stay well-formed."""

    def test_real_assets_pin_urls_and_checksums(self):
        for name, asset in pr.ASSETS.items():
            self.assertTrue(asset["url"].startswith("https://"), name)
            self.assertRegex(asset["sha256"], r"^[0-9a-f]{64}$", name)
            self.assertTrue(asset["filename"], name)


if __name__ == "__main__":
    unittest.main()
