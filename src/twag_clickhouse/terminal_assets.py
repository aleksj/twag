from __future__ import annotations

import hashlib
import pathlib
import shutil


TERMINAL_WEB_DIR = pathlib.Path(__file__).with_name("terminal_web")
APP_URL_PLACEHOLDER = "__TWAG_TERMINAL_APP_URL__"
STYLES_URL_PLACEHOLDER = "__TWAG_TERMINAL_STYLES_URL__"


def asset_version(path: pathlib.Path, *, length: int = 12) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:length]


def terminal_asset_versions(web_dir: pathlib.Path = TERMINAL_WEB_DIR) -> dict[str, str]:
    return {
        "app.js": asset_version(web_dir / "app.js"),
        "styles.css": asset_version(web_dir / "styles.css"),
    }


def terminal_asset_url(asset: str, version: str, *, asset_base: str = "/terminal") -> str:
    normalized_base = asset_base.strip()
    if normalized_base in {"", "."}:
        return f"{asset}?v={version}"
    return f"{normalized_base.rstrip('/')}/{asset}?v={version}"


def render_terminal_index(
    *,
    web_dir: pathlib.Path = TERMINAL_WEB_DIR,
    asset_base: str = "/terminal",
) -> str:
    versions = terminal_asset_versions(web_dir)
    html = (web_dir / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        STYLES_URL_PLACEHOLDER,
        terminal_asset_url("styles.css", versions["styles.css"], asset_base=asset_base),
    )
    html = html.replace(
        APP_URL_PLACEHOLDER,
        terminal_asset_url("app.js", versions["app.js"], asset_base=asset_base),
    )
    if APP_URL_PLACEHOLDER in html or STYLES_URL_PLACEHOLDER in html:
        raise RuntimeError("Terminal asset URL placeholder was not replaced")
    return html


def build_terminal_static(
    output_dir: pathlib.Path,
    *,
    web_dir: pathlib.Path = TERMINAL_WEB_DIR,
    asset_base: str = ".",
    clean: bool = False,
) -> None:
    if clean and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(
        render_terminal_index(web_dir=web_dir, asset_base=asset_base),
        encoding="utf-8",
    )
    for asset in ("app.js", "styles.css"):
        shutil.copy2(web_dir / asset, output_dir / asset)
