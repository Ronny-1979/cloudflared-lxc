import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(CFWEBUI_STATE=TMP + "/s", CFWEBUI_CONF=TMP + "/c", CFWEBUI_NO_SYSTEMD="1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import cfmanager as cf  # noqa: E402

BASE = {**json.loads(json.dumps(cf.DEFAULTS)),
        "main": {"hostname": "ha.example.de", "service": "192.168.1.5:8123", "no_tls_verify": True},
        "hosts": [{"hostname": "nas.example.de", "service": "192.168.1.6:5000", "disable_chunked": True}]}


class Tests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(cf.normalize_service("1.2.3.4:80"), "http://1.2.3.4:80")
        self.assertEqual(cf.normalize_service("https://x:1"), "https://x:1")
        self.assertEqual(cf.normalize_service(""), "")

    def test_label_not_in_config(self):
        s = json.loads(json.dumps(BASE))
        s["main"]["label"] = "Home Assistant"
        s["hosts"][0]["label"] = "NAS"
        self.assertEqual(cf.validate_settings(s), [])
        self.assertNotIn("Home Assistant", json.dumps(cf.build_config(s, "id", "/x.json")))

    def test_run_params(self):
        ok = json.loads(json.dumps(BASE)); ok["run_parameters"] = ["--protocol=http2", "--region=us"]
        self.assertEqual(cf.validate_settings(ok), [])
        for bad_param in ('--protocol=ht"tp2', "--protocol=a$b", "--protocol=a;b", "--unknown=1"):
            bad = json.loads(json.dumps(BASE)); bad["run_parameters"] = [bad_param]
            self.assertTrue(cf.validate_settings(bad), bad_param)

    def test_export_import(self):
        cur = json.loads(json.dumps(cf.DEFAULTS))
        s = json.loads(json.dumps(BASE)); s["main"]["label"] = "HA"
        exp = cf.export_settings(s)
        self.assertNotIn("github_repo", json.dumps(exp))
        new, errs = cf.import_settings(json.loads(json.dumps(exp)), cur)
        self.assertEqual(errs, [])
        self.assertEqual(new["main"]["label"], "HA")
        self.assertEqual(new["hosts"][0]["hostname"], "nas.example.de")
        self.assertEqual(new["github_repo"], cur["github_repo"])

    def test_import_rejects(self):
        cur = json.loads(json.dumps(cf.DEFAULTS))
        self.assertIsNone(cf.import_settings({"foo": 1}, cur)[0])
        evil = cf.export_settings(json.loads(json.dumps(BASE)))
        evil["settings"]["github_repo"] = "evil/repo"
        evil["settings"]["run_parameters"] = ["--protocol=a;b"]
        new, errs = cf.import_settings(evil, cur)
        self.assertIsNone(new); self.assertTrue(errs)
        evil["settings"]["run_parameters"] = []
        new, errs = cf.import_settings(evil, cur)
        self.assertEqual(new["github_repo"], cur["github_repo"])

    def test_disabled_entries(self):
        s = json.loads(json.dumps(BASE))
        s["hosts"][0]["enabled"] = False
        cfg = cf.build_config(s, "id", "/x.json")
        hosts = [r.get("hostname") for r in cfg["ingress"]]
        self.assertNotIn("nas.example.de", hosts)
        self.assertIn("ha.example.de", hosts)
        self.assertEqual(cf.all_hostnames(s, enabled_only=True), ["ha.example.de"])
        self.assertEqual(len(cf.all_hostnames(s)), 2)
        s["main"]["enabled"] = False
        self.assertNotIn("ha.example.de", [r.get("hostname") for r in cf.build_config(s, "id", "/x.json")["ingress"]])
        exp = cf.export_settings(s)
        new, errs = cf.import_settings(json.loads(json.dumps(exp)), json.loads(json.dumps(cf.DEFAULTS)))
        self.assertEqual(errs, [])
        self.assertFalse(new["hosts"][0]["enabled"]); self.assertFalse(new["main"]["enabled"])

    def test_valid(self):
        self.assertEqual(cf.validate_settings(BASE), [])

    def test_invalid(self):
        bad = json.loads(json.dumps(BASE))
        bad["main"]["hostname"] = "https://ha.example.de"
        bad["hosts"].append({"hostname": "nas.example.de", "service": "x y"})
        bad["run_parameters"] = ["--evil=1"]
        errs = cf.validate_settings(bad)
        self.assertGreaterEqual(len(errs), 3)

    def test_empty(self):
        self.assertTrue(cf.validate_settings(cf.load_settings()))

    def test_config(self):
        c = cf.build_config(BASE, "abc", "/x.json")
        self.assertEqual(c["ingress"][0]["service"], "http://192.168.1.5:8123")
        self.assertTrue(c["ingress"][1]["originRequest"]["disableChunkedEncoding"])
        self.assertEqual(c["ingress"][-1], {"service": "http_status:404"})

    def test_cloudflared_validates(self):
        exe = os.environ.get("CLOUDFLARED_BIN") or shutil.which("cloudflared")
        if not exe:
            self.skipTest("cloudflared fehlt")
        p = Path(TMP) / "c.json"
        p.write_text(json.dumps(cf.build_config(BASE, "00000000-0000-0000-0000-000000000000", "/x.json")))
        r = subprocess.run([exe, "tunnel", "--config", str(p), "ingress", "validate"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_tunnel_name_ignores_warnings(self):
        fake = Path(TMP) / "fake-cloudflared"
        fake.write_text("#!/bin/sh\necho 'WRN Your version is outdated' >&2\necho '[{\"name\":\"proxmox-tunnel\"}]'\n")
        fake.chmod(0o755)
        cf.CLOUDFLARED = str(fake)
        cf.CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        cf.CREDS_FILE.write_text('{"TunnelID": "abc"}')
        self.assertEqual(cf.existing_tunnel_name(), "proxmox-tunnel")

    def test_versions(self):
        self.assertGreater(cf.version_tuple("v1.2.10"), cf.version_tuple("1.2.9"))

    def test_env(self):
        cf.write_env(BASE)
        txt = cf.ENV_FILE.read_text()
        self.assertIn("run proxmox-tunnel", txt)
        self.assertTrue(txt.startswith('ARGS="tunnel '))


if __name__ == "__main__":
    unittest.main()
