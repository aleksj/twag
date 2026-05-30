from __future__ import annotations

import hashlib
import pathlib

from twag_clickhouse.terminal_assets import (
    TERMINAL_WEB_DIR,
    build_terminal_static,
    render_terminal_index,
    terminal_asset_versions,
)


def test_terminal_index_uses_content_hashed_asset_urls() -> None:
    versions = terminal_asset_versions()
    html = render_terminal_index(asset_base="/terminal")

    assert f'/terminal/app.js?v={versions["app.js"]}' in html
    assert f'/terminal/styles.css?v={versions["styles.css"]}' in html
    assert "__TWAG_TERMINAL_" not in html
    assert "202605" not in html


def test_build_terminal_static_writes_relative_hashed_assets(tmp_path: pathlib.Path) -> None:
    build_terminal_static(tmp_path, asset_base=".", clean=True)

    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    app_hash = hashlib.sha256((TERMINAL_WEB_DIR / "app.js").read_bytes()).hexdigest()[:12]
    css_hash = hashlib.sha256((TERMINAL_WEB_DIR / "styles.css").read_bytes()).hexdigest()[:12]

    assert f'app.js?v={app_hash}' in html
    assert f'styles.css?v={css_hash}' in html
    assert (tmp_path / "app.js").read_bytes() == (TERMINAL_WEB_DIR / "app.js").read_bytes()
    assert (tmp_path / "styles.css").read_bytes() == (
        TERMINAL_WEB_DIR / "styles.css"
    ).read_bytes()
