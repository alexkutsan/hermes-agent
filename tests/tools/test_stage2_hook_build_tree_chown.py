"""Contract test: the s6-overlay stage2 hook repairs build-tree ownership.

Regression guard for the HERMES_UID/PUID remap path broken by #35027.

`usermod -u <new> hermes` re-chowns the hermes home dir ($HERMES_HOME ==
/opt/data) to the new UID as a side effect. #35027 gated the build-tree chown
behind `stat $HERMES_HOME != hermes_uid`, so after any remap that stat is
already satisfied and the build-tree chown was silently skipped — leaving
.venv owned by the build-time UID (10000) and breaking:
  - lazy_deps.py `uv pip install` of platform extras (#15012, #21100)
  - the TUI esbuild rebuild into ui-tui/dist (#28851)

The current fix avoids an unconditional `chown -R` over the full .venv and
node_modules trees. Stage2 scans each runtime-writable build tree for entries
whose UID/GID does not match the runtime hermes user and chowns only those
entries. Settled restarts still scan metadata, but they do not dirty every
inode again.

The extraction + stubbed-shell-run approach mirrors
tests/tools/test_stage2_hook_toplevel_chown.py.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE2_HOOK = REPO_ROOT / "docker" / "stage2-hook.sh"


@pytest.fixture(scope="module")
def stage2_text() -> str:
    if not STAGE2_HOOK.exists():
        pytest.skip("docker/stage2-hook.sh not present in this checkout")
    return STAGE2_HOOK.read_text()


def _build_tree_chown_block(text: str) -> str:
    """Extract the build-tree targeted chown block."""
    m = re.search(
        r"(repair_install_tree_owners\(\) \{\n(?:.*\n)*?^done)",
        text,
        flags=re.MULTILINE,
    )
    assert m, "stage2-hook.sh must contain the build-tree targeted chown block"
    return m.group(1)


def test_build_tree_chown_not_gated_on_hermes_home(stage2_text: str) -> None:
    """The build-tree chown must NOT live inside the `if [ "$needs_chown" = true ]`
    block keyed on $HERMES_HOME ownership — that is exactly the #35027 bug."""
    block = _build_tree_chown_block(stage2_text)
    assert "$HERMES_HOME" not in block
    assert 'find "$tree"' in block
    assert '! -uid "$actual_hermes_uid"' in block
    assert '! -gid "$actual_hermes_gid"' in block
    # All runtime-writable build trees are covered independently.
    for tree in (
        "$INSTALL_DIR/.venv",
        "$INSTALL_DIR/ui-tui",
        "$INSTALL_DIR/gateway",
        "$INSTALL_DIR/node_modules",
    ):
        assert tree in block, f"build-tree chown must cover {tree}"
    assert "chown -R hermes:hermes" not in block


def _run_build_tree_chown_block(
    text: str,
    *,
    hermes_uid: int = 4242,
    hermes_gid: int = 4243,
    mismatch: bool,
) -> list[str]:
    """Run the extracted block with external commands stubbed."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    block = _build_tree_chown_block(text)

    with tempfile.TemporaryDirectory() as d:
        dpath = Path(d)
        log = dpath / "chown.log"
        install_dir = dpath / "install"
        for rel in (".venv", "ui-tui", "gateway", "node_modules"):
            (install_dir / rel).mkdir(parents=True)

        mismatch_output = f"{install_dir}/.venv/bad"
        script = (
            "set -eu\n"
            f'INSTALL_DIR="{install_dir}"\n'
            f'actual_hermes_uid={hermes_uid}\n'
            f'actual_hermes_gid={hermes_gid}\n'
            "stat() { echo \"$actual_hermes_uid:$actual_hermes_gid\"; }\n"
            "find() {\n"
            "  case \" $* \" in\n"
            "    *\" -print -quit \"*)\n"
            f"      {'echo ' + mismatch_output if mismatch else 'return 0'}\n"
            "      ;;\n"
            "    *\" -exec chown -h hermes:hermes {} + \"*)\n"
            f'      echo "targeted-find-chown $*" >> "{log}"\n'
            "      ;;\n"
            "    *) return 1 ;;\n"
            "  esac\n"
            "}\n"
            f'chown() {{ echo "direct-chown $*" >> "{log}"; }}\n'
            + block
        )
        script_path = dpath / "harness.sh"
        script_path.write_text(script)
        proc = subprocess.run([bash, str(script_path)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return log.read_text().splitlines() if log.exists() else []


def test_targeted_chown_fires_when_entries_differ(stage2_text: str) -> None:
    """The #35027 regression scenario: after a remap $HERMES_HOME already
    matches the new UID, but install-tree entries still carry the build-time
    UID/GID (10000). Stage2 must repair them independently of $HERMES_HOME."""
    lines = _run_build_tree_chown_block(
        stage2_text,
        mismatch=True,
    )
    assert any(line.startswith("targeted-find-chown ") for line in lines), (
        "build-tree chown must use find to repair mismatched entries"
    )
    assert not any("chown -R" in line for line in lines)


def test_chown_skipped_when_no_mismatched_entries(stage2_text: str) -> None:
    """Idempotency: once entries are hermes-owned, no chown commands run on
    subsequent boots."""
    lines = _run_build_tree_chown_block(
        stage2_text,
        mismatch=False,
    )
    assert not lines


def test_targeted_chown_uses_symlink_safe_chown(stage2_text: str) -> None:
    block = _build_tree_chown_block(stage2_text)
    assert "-exec chown -h hermes:hermes {} +" in block

