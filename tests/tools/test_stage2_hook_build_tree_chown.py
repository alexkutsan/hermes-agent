"""Contract test: the s6-overlay stage2 hook keeps build trees writable.

Regression guard for the HERMES_UID/PUID remap path broken by #35027.

`usermod -u <new> hermes` re-chowns the hermes home dir ($HERMES_HOME ==
/opt/data) to the new UID as a side effect. #35027 gated the build-tree chown
behind `stat $HERMES_HOME != hermes_uid`, so after any remap that stat is
already satisfied and the build-tree chown was silently skipped — leaving
.venv owned by the build-time UID (10000) and breaking:
  - lazy_deps.py `uv pip install` of platform extras (#15012, #21100)
  - the TUI esbuild rebuild into ui-tui/dist (#28851)

The current fix avoids a startup-scale `chown -R` over the full .venv and
node_modules trees. New Docker layers make those trees group-writable; stage2
recreates the old build GID as a supplemental group for hermes after GID remap,
then only falls back to directory-owner repair when the trees still are not
writable.

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


def _build_tree_repair_block(text: str) -> str:
    """Extract the build-tree repair block."""
    m = re.search(
        r"(ensure_install_tree_group\(\) \{\n(?:.*\n)*?^done)",
        text,
        flags=re.MULTILINE,
    )
    assert m, "stage2-hook.sh must contain the build-tree writability repair block"
    return m.group(1)


def test_build_tree_repair_not_gated_on_hermes_home(stage2_text: str) -> None:
    """The build-tree repair must NOT live inside the `if [ "$needs_chown" = true ]`
    block keyed on $HERMES_HOME ownership — that is exactly the #35027 bug."""
    block = _build_tree_repair_block(stage2_text)
    assert "$HERMES_HOME" not in block
    assert "as_hermes test -w" in block
    # All runtime-writable build trees are covered independently.
    for tree in (
        "$INSTALL_DIR/.venv",
        "$INSTALL_DIR/ui-tui",
        "$INSTALL_DIR/gateway",
        "$INSTALL_DIR/node_modules",
    ):
        assert tree in block, f"build-tree repair must cover {tree}"
    assert "chown -R hermes:hermes" not in block


def _run_build_tree_repair_block(
    text: str,
    *,
    tree_gid: int,
    hermes_uid: int = 4242,
    hermes_gid: int = 4243,
    writable_as_hermes: bool,
) -> list[str]:
    """Run the extracted block with external commands stubbed."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    block = _build_tree_repair_block(text)

    with tempfile.TemporaryDirectory() as d:
        dpath = Path(d)
        log = dpath / "chown.log"
        install_dir = dpath / "install"
        for rel in (".venv", "ui-tui", "gateway", "node_modules"):
            (install_dir / rel).mkdir(parents=True)

        writable = "0" if writable_as_hermes else "1"
        script = (
            "set -eu\n"
            f'INSTALL_DIR="{install_dir}"\n'
            f'actual_hermes_uid={hermes_uid}\n'
            f'actual_hermes_gid={hermes_gid}\n'
            "stat() { echo "
            f"{tree_gid}"
            "; }\n"
            "id() {\n"
            "  if [ \"$1\" = \"-G\" ]; then echo \"4243\"; return 0; fi\n"
            "  if [ \"$1\" = \"-g\" ]; then echo \"$actual_hermes_gid\"; return 0; fi\n"
            "  if [ \"$1\" = \"-u\" ]; then echo \"$actual_hermes_uid\"; return 0; fi\n"
            "  return 1\n"
            "}\n"
            "getent() { return 2; }\n"
            f'groupadd() {{ echo "groupadd $*" >> "{log}"; }}\n'
            f'usermod() {{ echo "usermod $*" >> "{log}"; }}\n'
            f'chown() {{ echo "chown $*" >> "{log}"; }}\n'
            f'find() {{ echo "find $*" >> "{log}"; }}\n'
            f'as_hermes() {{ return {writable}; }}\n'
            + block
        )
        script_path = dpath / "harness.sh"
        script_path.write_text(script)
        proc = subprocess.run([bash, str(script_path)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return log.read_text().splitlines() if log.exists() else []


def test_build_gid_group_is_granted_when_tree_gid_differs(stage2_text: str) -> None:
    """The #35027 regression scenario: after a remap $HERMES_HOME already
    matches the new UID, but install trees still carry the build-time GID
    (10000). Stage2 must grant hermes that GID independently of $HERMES_HOME."""
    lines = _run_build_tree_repair_block(
        stage2_text,
        tree_gid=10000,
        hermes_gid=4243,
        writable_as_hermes=True,
    )
    assert any(line == "groupadd -g 10000 hermesbuild" for line in lines)
    assert any(line == "usermod -aG hermesbuild hermes" for line in lines)
    assert not any(line.startswith("chown ") for line in lines), (
        "group-writable build trees should not need startup chown"
    )


def test_directory_chown_fallback_when_group_write_is_insufficient(stage2_text: str) -> None:
    """If group membership does not make a tree writable, repair directories
    rather than recursively chowning every dependency file."""
    lines = _run_build_tree_repair_block(
        stage2_text,
        tree_gid=10000,
        hermes_gid=4243,
        writable_as_hermes=False,
    )
    assert any(line.startswith("chown hermes:hermes ") for line in lines)
    assert any(" -type d " in f" {line} " for line in lines)
    assert not any(line.startswith("chown -R ") for line in lines)


def test_group_grant_skipped_when_tree_gid_already_matches(stage2_text: str) -> None:
    """No remap: tree GID and hermes GID already match."""
    lines = _run_build_tree_repair_block(
        stage2_text,
        tree_gid=10000,
        hermes_gid=10000,
        writable_as_hermes=True,
    )
    assert not any(line.startswith(("groupadd ", "usermod ", "chown ", "find ")) for line in lines)

