"""#13 설치·운영: 다른 폴더 실행, 설정 파일 인코딩, 인자 기본값, raw 매니페스트, 버전."""
import importlib.util
import io
import json
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

from conftest import SCRIPTS, add_paper
from lra import __version__, config, verify as V
from lra.config import Paths, load_config

spec = importlib.util.spec_from_file_location("lra_cli", SCRIPTS / "lra.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def test_global_install_with_root(tmp_path):
    """스킬 폴더를 다른 곳(전역 설치)에 두고, 또 다른 폴더를 --root로 지정해 status가 동작."""
    skill = tmp_path / "home/.claude/skills/adaptive-literature-review"
    shutil.copytree(SCRIPTS.parent, skill, ignore=shutil.ignore_patterns("__pycache__"))
    research, elsewhere = tmp_path / "research", tmp_path / "elsewhere"
    elsewhere.mkdir()
    lra = skill / "scripts/lra.py"
    r = subprocess.run([sys.executable, str(lra), "--root", str(research), "init", "--title", "t"],
                       cwd=elsewhere, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sys.executable, str(lra), "--root", str(research), "status"],
                       cwd=elsewhere, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert (research / "state/literature.db").exists() and not (elsewhere / "state").exists()


def test_skill_uses_skill_dir_variable():
    text = (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")
    assert '`LRA` = `python "${CLAUDE_SKILL_DIR}/scripts/lra.py"`' in text


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_config_reads_utf8_with_or_without_bom(tmp_path, encoding):
    paths = Paths(tmp_path)
    paths.ensure()
    paths.config.write_text(json.dumps({"mailto": "a@b.c"}), encoding=encoding)
    assert load_config(paths)["mailto"] == "a@b.c"


def test_config_utf16_gives_clear_error(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    paths.config.write_text(json.dumps({"mailto": "a@b.c"}), encoding="utf-16")
    with pytest.raises(SystemExit, match="UTF-16"):
        load_config(paths)


def _run(fn, paths, **kw):
    with redirect_stdout(io.StringIO()):
        fn(SimpleNamespace(root=str(paths.root), no_cache=True, **kw))


def test_mode_set_requires_mode(project):
    paths, con = project
    with pytest.raises(SystemExit, match="모드가 필요"):
        _run(cli.cmd_mode, paths, action="set", mode=None, reason="x", domestic_share=None)


def test_rank_pin_requires_rank(project):
    paths, con = project
    pid = add_paper(con, "Pin target paper")
    with pytest.raises(SystemExit, match="--rank"):
        _run(cli.cmd_rank, paths, action="pin", papers=[pid], file=None, rank=None, reason="x", by="user")


def test_manifest_ignores_os_and_lock_files(project):
    paths, con = project
    (paths.raw / "plan.docx").write_bytes(b"plan")
    paths.raw_manifest.write_text(json.dumps(V.raw_manifest(paths)), encoding="utf-8")
    for junk in (".DS_Store", "~$plan.docx", "Thumbs.db"):
        (paths.raw / junk).write_bytes(b"x")
    assert set(V.raw_manifest(paths)) == {"raw/plan.docx"}
    raw = next(ln for ln in V.run(con, paths)[0].splitlines() if ln.startswith("| RAW"))
    assert "**PASS**" in raw


def test_old_manifest_with_junk_entries_still_passes(project):
    paths, con = project
    (paths.raw / "plan.docx").write_bytes(b"plan")
    m = V.raw_manifest(paths)
    m["raw/.DS_Store"] = "deadbeef"   # 이전 버전이 기록한 부산물
    paths.raw_manifest.write_text(json.dumps(m), encoding="utf-8")
    raw = next(ln for ln in V.run(con, paths)[0].splitlines() if ln.startswith("| RAW"))
    assert "**PASS**" in raw


def test_version_single_source():
    assert config.USER_AGENT.startswith(f"adaptive-literature-review/{__version__} ")
    major_minor = ".".join(__version__.split(".")[:2])
    assert f"(v{major_minor})" in (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPTS / "lra.py"), "--version"], capture_output=True, text=True)
    assert __version__ in r.stdout


def test_video_not_shipped():
    repo = SCRIPTS.parents[3]
    assert "소개영상.mp4" not in (repo / ".gitignore").read_text(encoding="utf-8")
