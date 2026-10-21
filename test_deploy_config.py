"""Offline regression checks for deployment paths and validation origins."""

import copy
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parent


class DeploymentConfigTests(unittest.TestCase):
    def test_activation_snapshot_validation(self) -> None:
        host = "proxy.example.ts.net:443"
        other_host = "proxy.example.ts.net:8443"
        route = {"Proxy": "http://127.0.0.1:8087/mta"}
        script = (REPOSITORY / "deploy/activate.sh").read_text()
        validation = script.split(
            'python3 - "$snapshot" "$root/snapshots/$stamp-after.json" <<\'PY\'\n'
        )[1].split("\nPY\n")[0]
        snapshots = (
            {},
            {
                "TCP": {"8443": {"HTTPS": True}},
                "Web": {other_host: {"Handlers": {"/": {"Text": "other port"}}}},
                "AllowFunnel": {other_host: True},
            },
            {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {host: {"Handlers": {"/": {"Text": "existing route"}}}},
                "AllowFunnel": {host: True},
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            before_path = Path(temporary) / "before.json"
            after_path = Path(temporary) / "after.json"
            for before in snapshots:
                after = copy.deepcopy(before)
                after.setdefault("TCP", {}).setdefault("443", {"HTTPS": True})
                handlers = after.setdefault("Web", {}).setdefault(host, {}).setdefault("Handlers", {})
                handlers["/mta"] = route
                after.setdefault("AllowFunnel", {})[host] = True
                for changed_unrelated_route in (False, True):
                    with self.subTest(before=before, changed=changed_unrelated_route):
                        candidate = copy.deepcopy(after)
                        if changed_unrelated_route:
                            candidate["Web"][host]["Handlers"]["/"] = {"Text": "unexpected change"}
                        before_path.write_text(json.dumps(before))
                        after_path.write_text(json.dumps(candidate))
                        result = subprocess.run(
                            [sys.executable, "-", str(before_path), str(after_path)],
                            input=validation, text=True, capture_output=True,
                            env={**os.environ, "MTA_PROXY_PUBLIC_HOST": host.removesuffix(":443")},
                        )
                        if changed_unrelated_route:
                            self.assertNotEqual(result.returncode, 0)
                            self.assertIn("Existing Funnel configuration changed", result.stderr)
                        else:
                            self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_root_and_custom_unit_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "release"
            shutil.copytree(REPOSITORY / "deploy", release / "deploy")
            environment = {"HOME": str(root), "PATH": os.environ["PATH"]}
            command = 'source "$1/deploy/config.sh"; printf "%s" "$root"; install_units "$1"'
            default = subprocess.run(
                ["bash", "-euc", command, "test", str(release)],
                env=environment, check=True, text=True, capture_output=True,
            )
            self.assertEqual(default.stdout, str(root / "mta-proxy"))
            custom = root / "custom-proxy"
            subprocess.run(
                ["bash", "-euc", command, "test", str(release)],
                env={**environment, "MTA_PROXY_ROOT": str(custom)},
                check=True, text=True, capture_output=True,
            )
            units = root / ".config/systemd/user"
            for name in ("mta-proxy.service", "mta-proxy-health.service"):
                contents = (units / name).read_text()
                self.assertIn(f"WorkingDirectory={custom}/current", contents)
                self.assertIn(f"ExecStart={custom}/current/.venv/bin/python", contents)
                self.assertNotIn("%h/mta-proxy", contents)

    def test_rejects_unsafe_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            shutil.copytree(REPOSITORY / "deploy", release / "deploy")
            for root in ("relative/path", "/srv/space here", "/srv/$variable", "/srv/a|b"):
                with self.subTest(root=root):
                    result = subprocess.run(
                        ["bash", "-euc", 'source "$1/deploy/config.sh"', "test", str(release)],
                        env={"HOME": temporary, "PATH": os.environ["PATH"], "MTA_PROXY_ROOT": root},
                        text=True, capture_output=True,
                    )
                    self.assertEqual(result.returncode, 2)

    def test_validation_origin(self) -> None:
        for value, expected in (
            (None, "http://127.0.0.1:8087"),
            ("https://proxy.example.ts.net/", "https://proxy.example.ts.net"),
        ):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["MTA_PROXY_BASE"] = value
                module = runpy.run_path(str(REPOSITORY / "verify_hosted.py"))
                self.assertEqual(module["BASE"], expected)
