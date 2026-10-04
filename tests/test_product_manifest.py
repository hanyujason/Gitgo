from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.render_product_manifest import render_manifest, render_shell_contract


def test_product_manifest_renders_platform_specific_layouts():
    root = Path(__file__).resolve().parents[1]
    source = root / "packaging" / "product.json"

    macos = render_manifest(source, "macos", "arm64")
    windows = render_manifest(source, "windows", "x86_64")
    linux = render_manifest(source, "linux", "x86_64")

    assert "layouts" not in macos
    assert macos["platform"] == "macos"
    assert macos["architecture"] == "arm64"
    assert macos["layout"]["dashboard"] == "gitgo"
    assert windows["layout"]["dashboard"] == "gitgo.exe"
    assert linux["layout"]["native_host"] == "internal/gitgo-host/gitgo-host"


def test_product_manifest_renders_shell_safe_installer_contract():
    contract = render_shell_contract({
        "platform": "linux",
        "architecture": "x86_64",
        "primary_command": "gitgo",
        "command_aliases": ["gitgo-preview"],
    })

    assert "GITGO_PRODUCT_PLATFORM=linux\n" in contract
    assert "GITGO_PRODUCT_ARCHITECTURE=x86_64\n" in contract
    assert "GITGO_PRODUCT_COMMAND=gitgo\n" in contract
    assert "GITGO_PRODUCT_ALIASES=gitgo-preview\n" in contract


def test_product_manifest_rejects_unknown_platform(tmp_path_factory):
    source = tmp_path_factory / "product.json"
    source.write_text(json.dumps({
        "primary_command": "gitgo",
        "layouts": {"macos": {"dashboard": "gitgo"}},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="no layout"):
        render_manifest(source, "linux")
