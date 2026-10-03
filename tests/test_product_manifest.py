from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.render_product_manifest import render_manifest


def test_product_manifest_renders_platform_specific_layouts():
    root = Path(__file__).resolve().parents[1]
    source = root / "packaging" / "product.json"

    macos = render_manifest(source, "macos", "arm64")
    windows = render_manifest(source, "windows", "x86_64")

    assert "layouts" not in macos
    assert macos["platform"] == "macos"
    assert macos["architecture"] == "arm64"
    assert macos["layout"]["dashboard"] == "gitgo"
    assert windows["layout"]["dashboard"] == "gitgo.exe"


def test_product_manifest_rejects_unknown_platform(tmp_path_factory):
    source = tmp_path_factory / "product.json"
    source.write_text(json.dumps({
        "primary_command": "gitgo",
        "layouts": {"macos": {"dashboard": "gitgo"}},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="no layout"):
        render_manifest(source, "linux")
