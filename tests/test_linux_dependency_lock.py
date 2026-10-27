"""Offline lock/inventory validation; no package download or installation."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def verifier():
    path = Path(__file__).resolve().parents[1] / "scripts/verify_linux_dependencies.py"
    spec = importlib.util.spec_from_file_location("verify_linux_dependencies", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pin(name="Example_package", version="1.2.3"):
    return f"{name}=={version} --hash=sha256:{'a' * 64}"


def test_strict_lock_accepts_hash_continuations(verifier):
    text = f"# Official distribution hashes\nExample_package==1.2.3 \\\n    --hash=sha256:{'a' * 64} \\\n    --hash=sha256:{'b' * 64}\n"
    assert verifier.read_lock(text) == {"example-package": "1.2.3"}


@pytest.mark.parametrize("text", [
    "", "example>=1.0", "example==1.0", "--index-url https://example.invalid/simple",
    "example @ https://example.invalid/package.whl", pin() + "; python_version>'3'",
    pin() + " --extra-index-url https://example.invalid", pin() + "\n" + pin(),
    "example==1.0 --hash=sha256:short", "example==1.0 \\",
    "example==1.0 \\\n# interrupted\n" + pin(),
])
def test_unsafe_or_unlocked_requirement_rejected(verifier, text):
    with pytest.raises(ValueError):
        verifier.read_lock(text)


@pytest.mark.parametrize("installed", [
    [], [("example", "2.0")], [("example", "1.0"), ("unlisted", "1.0")],
    [("example", "1.0"), ("EXAMPLE", "1.0")],
])
def test_inventory_mismatch_rejected(verifier, installed):
    with pytest.raises(ValueError):
        verifier.validate_inventory({"example": "1.0"}, installed)


def test_exact_inventory_accepts_venv_pip(verifier):
    verifier.validate_inventory({"example-package": "1.0"},
                                [("Example_package", "1.0"), ("pip", "26.0")])


def test_real_locks_cover_original_pins_and_linux_dependencies(verifier):
    root = Path(__file__).resolve().parents[1]
    expected = verifier.expected_versions(root)
    for line in (root / "requirements.lock").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, version = line.split("==")
        assert expected[verifier.canonical(name)] == version
    assert expected["py-mini-racer"] == "0.6.0"
    assert expected["akracer"] == "0.0.14"
    assert "setuptools" in expected and "wheel" in expected


def test_installer_never_resolves_build_tools_or_editable_project():
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts/install_linux_dependencies.sh").read_text()
    assert text.count("--require-hashes") == 2
    assert "--no-build-isolation" in text and "--isolated check" in text
    assert "python -m venv" in text and "test ! -e" in text
    assert "pip install -e" not in text and "--no-deps -e" not in text
    assert "ASHARE_STORAGE_TOKEN" not in text
