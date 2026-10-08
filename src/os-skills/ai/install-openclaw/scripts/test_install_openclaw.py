"""The installer must launch OpenClaw CLI children against the selected --config.

OpenClaw resolves its configuration through OPENCLAW_CONFIG_PATH. When the
installer only writes the selected file and never exports the variable, every
child command it launches (gateway stop/install/restart, the write-scope
smoke test, the status probe, plugin installs) reads the default
configuration or an unrelated inherited override instead - the smoke check
then validates a different configuration from the one just written
(https://github.com/agentic-os-org/ANOLISA/issues/6558).
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPT_PATH = Path(__file__).with_name("install_openclaw.py")


def load_installer():
    spec = importlib.util.spec_from_file_location("install_openclaw", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recording_run(seen):
    """subprocess.run stand-in that records (cmd, OPENCLAW_CONFIG_PATH) pairs."""

    def run(cmd, **kwargs):
        env = kwargs.get("env")
        seen.append((tuple(cmd), None if env is None else env.get("OPENCLAW_CONFIG_PATH")))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    return run


def recording_command(seen):
    """run_command stand-in that records (cmd, env) pairs."""

    def run(cmd, **kwargs):
        seen.append((tuple(cmd), kwargs.get("env")))
        return subprocess.CompletedProcess(cmd, 0)

    return run


def test_gateway_children_receive_selected_config(tmp_path):
    installer = load_installer()
    selected = tmp_path / "selected.json"
    seen = []
    argv = [
        "installer",
        "--config",
        str(selected),
        "--api-key",
        "test-placeholder",
        "--skip-preflight",
        "--skip-install-openclaw",
    ]
    with (
        mock.patch.dict(os.environ),
        mock.patch.object(sys, "argv", argv),
        mock.patch.object(installer, "dependency_precheck"),
        mock.patch.object(installer, "clear_openclaw_gateway_port"),
        mock.patch.object(installer, "wait_gateway_ready"),
        mock.patch.object(subprocess, "run", side_effect=recording_run(seen)),
    ):
        os.environ.pop("OPENCLAW_CONFIG_PATH", None)
        installer.main()

    assert selected.is_file(), "the selected config must still be written"
    openclaw_calls = [entry for entry in seen if entry[0][0] == "openclaw"]
    assert openclaw_calls, "expected the installer to launch OpenClaw commands"
    joined = " ".join(" ".join(cmd) for cmd, _ in openclaw_calls)
    assert "gateway stop" in joined
    assert "gateway install" in joined
    assert "gateway restart" in joined
    assert "agent --message hello" in joined
    for cmd, override in openclaw_calls:
        assert override == str(selected), (
            f"{' '.join(cmd)} ran with OPENCLAW_CONFIG_PATH={override!r}"
        )


def test_gateway_status_probe_uses_selected_config():
    installer = load_installer()
    seen = []
    args = SimpleNamespace(
        config="sel.json",
        dry_run=False,
        gateway_port=18789,
        gateway_ready_timeout=0,
        gateway_status_timeout=8,
        gateway_log="/nonexistent/install-openclaw-gateway.log",
    )
    with (
        mock.patch.object(installer.time, "monotonic", side_effect=[1.0, 2.0]),
        mock.patch.object(installer, "run_command", side_effect=recording_command(seen)),
    ):
        installer.wait_gateway_ready(args)

    openclaw_calls = [entry for entry in seen if entry[0][0] == "openclaw"]
    (cmd, env), = openclaw_calls
    assert cmd[1:4] == ("gateway", "status", "--deep")
    assert env["OPENCLAW_CONFIG_PATH"] == str(Path("sel.json").expanduser())


def test_dingtalk_plugin_install_uses_selected_config():
    installer = load_installer()
    seen = []
    args = SimpleNamespace(config="sel.json", npm_registry="https://registry.example", dry_run=False)
    with mock.patch.object(installer, "run_command", side_effect=recording_command(seen)):
        installer.install_dingtalk_plugin(args)

    (cmd, env), = seen
    assert cmd[0:3] == ("openclaw", "plugins", "install")
    assert env["OPENCLAW_CONFIG_PATH"] == str(Path("sel.json").expanduser())
    assert env["NPM_CONFIG_REGISTRY"] == "https://registry.example"


def test_tokenless_plugin_env_carries_selected_config(tmp_path):
    installer = load_installer()
    adapter = tmp_path / "adapter"
    install_script = adapter / "openclaw" / "scripts" / "install.sh"
    install_script.parent.mkdir(parents=True)
    (adapter / "manifest.json").write_text("{}", encoding="utf-8")
    install_script.write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
    seen = []
    args = SimpleNamespace(
        config=str(tmp_path / "selected.json"), skip_tokenless=False, dry_run=False
    )
    with (
        mock.patch.object(installer, "find_tokenless_adapter_dir", return_value=adapter),
        mock.patch.object(installer, "run_command", side_effect=recording_command(seen)),
    ):
        installer.install_tokenless_plugin(args)

    (cmd, env), = seen
    assert cmd[0:2] == ("bash", str(install_script))
    assert env["OPENCLAW_CONFIG_PATH"] == str(Path(args.config).expanduser())
    assert env["ANOLISA_TARGET"] == "openclaw"


def test_openclaw_env_overrides_inherited_value():
    installer = load_installer()
    args = SimpleNamespace(config="sel.json")
    with mock.patch.dict(os.environ, {"OPENCLAW_CONFIG_PATH": "/unrelated/override.json"}):
        env = installer.openclaw_env(args)

    assert env["OPENCLAW_CONFIG_PATH"] == str(Path("sel.json").expanduser())
