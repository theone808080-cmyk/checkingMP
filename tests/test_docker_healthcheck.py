from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]


class DockerHealthcheckSmokeTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("docker"):
            self.skipTest("Docker is not installed")
        available = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
        if available.returncode != 0:
            self.skipTest("Docker daemon is not available")

        self.image_tag = f"checkingmp-healthcheck-smoke-{uuid.uuid4().hex}"
        existing = subprocess.run(
            ["docker", "image", "inspect", self.image_tag],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
        if existing.returncode == 0:
            self.skipTest("Generated Docker image tag already exists")
        self.image_created = False

    def tearDown(self):
        if getattr(self, "image_created", False):
            subprocess.run(
                ["docker", "image", "rm", "--force", self.image_tag],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )

    def test_built_image_has_curl_for_healthcheck(self):
        build = subprocess.run(
            ["docker", "build", "--tag", self.image_tag, str(ROOT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=900,
        )
        self.assertEqual(build.returncode, 0, "Docker image build failed")
        self.image_created = True

        curl = subprocess.run(
            ["docker", "run", "--rm", self.image_tag, "curl", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        self.assertEqual(curl.returncode, 0, "curl is unavailable in the image")
