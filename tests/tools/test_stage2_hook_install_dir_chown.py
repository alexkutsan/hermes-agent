"""Contract test: stage2-hook keeps the gateway install tree writable.

When HERMES_UID is remapped at container boot, ``usermod -u`` only rewrites
files under the hermes user's home directory ($HERMES_HOME == /opt/data).
Runtime-writable trees under ``/opt/hermes`` must be writable before services
drop privileges. ``/opt/hermes/gateway`` is one such tree: Python writes
``__pycache__`` beneath the package on first import, which fails with EACCES if
the tree is not writable after a remap (#27221).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE2_HOOK = REPO_ROOT / "docker" / "stage2-hook.sh"


@pytest.fixture(scope="module")
def stage2_text() -> str:
    if not STAGE2_HOOK.exists():
        pytest.skip("docker/stage2-hook.sh not present in this checkout")
    return STAGE2_HOOK.read_text()


def _install_dir_repair_block(text: str) -> str:
    match = re.search(
        r"(ensure_install_tree_group\(\) \{\n(?:.*\n)*?^done)",
        text,
        flags=re.MULTILINE,
    )
    assert match, "stage2-hook.sh must repair runtime-writable install trees"
    return match.group(1)


def test_uid_remap_repairs_runtime_writable_gateway_tree(stage2_text: str) -> None:
    block = _install_dir_repair_block(stage2_text)
    assert '"$INSTALL_DIR/gateway"' in block, (
        "the build-tree repair must cover $INSTALL_DIR/gateway so the gateway "
        "runtime can write Python cache artifacts after a UID remap (#27221)"
    )


def test_install_dir_repair_keeps_existing_runtime_writable_trees(stage2_text: str) -> None:
    block = _install_dir_repair_block(stage2_text)
    for required in (
        '"$INSTALL_DIR/.venv"',
        '"$INSTALL_DIR/ui-tui"',
        '"$INSTALL_DIR/node_modules"',
    ):
        assert required in block
