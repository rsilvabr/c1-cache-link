import hashlib
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import c1_cache_link as c  # noqa: E402

HOSTILE = "250118_吾妻山公園_菜の花 Canola [FINAL] 50%"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "MAP_COPY", str(tmp_path / "home" / ".c1_cache_map.tsv"))
    monkeypatch.setattr(c, "capture_one_running", lambda: "")


def make_event(base, name=HOSTILE, photos=3):
    event = base / name
    c1 = event / "CaptureOne"
    for i in range(photos):
        stem = f"DSC_{i:04d} (2)"
        (event / f"{stem}.NEF").parent.mkdir(parents=True, exist_ok=True)
        (event / f"{stem}.NEF").write_bytes(os.urandom(2000))
        for sub, ext, size in (("Settings1674", ".cos", 300), ("Cache/Thumbnails", ".cot", 5000),
                               ("Cache/Proxies", ".cof", 9000)):
            path = c1 / sub / f"{stem}.NEF{ext}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(os.urandom(size))
    return event


def snapshot(root, skip_cache=True):
    """hash of every file below root, without following junctions."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not c.is_link(os.path.join(dirpath, d))
                       and not (skip_cache and d == "Cache")]
        for f in filenames:
            p = os.path.join(dirpath, f)
            out[os.path.relpath(p, root)] = hashlib.md5(open(p, "rb").read()).hexdigest()
    return out


def cache_hashes(cache_dir):
    out = {}
    for dirpath, _, filenames in os.walk(cache_dir):
        for f in filenames:
            p = os.path.join(dirpath, f)
            out[os.path.relpath(p, cache_dir)] = hashlib.md5(open(p, "rb").read()).hexdigest()
    return out


def run(*argv):
    return c.main([str(a) for a in argv])


def test_dry_run_changes_nothing(tmp_path):
    src = tmp_path / "G"
    make_event(src)
    before = snapshot(src, skip_cache=False)
    assert run("link", "--source", src, "--to", tmp_path / "fast") == 0
    assert snapshot(src, skip_cache=False) == before
    assert not (tmp_path / "fast").exists()


def test_link_moves_cache_and_keeps_settings(tmp_path):
    src = tmp_path / "G"
    event = make_event(src)
    cache = event / "CaptureOne" / "Cache"
    thumbs = cache_hashes(cache)
    settings = snapshot(src)
    root = tmp_path / "fast"
    assert run("link", "--source", src, "--to", root, "--apply") == 0
    assert c.is_link(cache)
    target = c.link_target(str(cache))
    assert c._same(target, c.mirror(str(root), str(cache)))
    assert cache_hashes(target) == thumbs
    assert cache_hashes(cache) == thumbs          # read through the junction
    assert snapshot(src) == settings              # edits and photos untouched
    assert not os.path.exists(str(cache) + c.STAGING_SUFFIX)
    # a write through the junction lands on the fast disk
    (cache / "Thumbnails" / "new.cot").write_bytes(b"x")
    assert os.path.exists(os.path.join(target, "Thumbnails", "new.cot"))
    # map written in the root and in the home copy
    text = (root / c.MAP_FILE_NAME).read_text(encoding="utf-8")
    assert str(cache) in text and "linked" in text
    assert os.path.exists(c.MAP_COPY)
    # second run: nothing to do
    assert run("link", "--source", src, "--to", root, "--apply") == 0


def test_link_discard_and_no_cache_folder(tmp_path):
    src = tmp_path / "G"
    event = make_event(src)
    other = make_event(src, "251223_São Luís [x]", photos=1)
    shutil.rmtree(other / "CaptureOne" / "Cache")
    root = tmp_path / "fast"
    assert run("link", "--source", src, "--to", root, "--discard", "--apply") == 0
    for ev in (event, other):
        cache = ev / "CaptureOne" / "Cache"
        assert c.is_link(cache)
        assert os.listdir(c.link_target(str(cache))) == []


def test_scan_skips_cache_root_inside_source(tmp_path):
    src = tmp_path / "G"
    make_event(src)
    root = src / "C1_Cache"
    assert run("link", "--source", src, "--to", root, "--apply") == 0
    items, errors = c.scan([str(src)], skip_roots=[], quiet=True)
    assert len(items) == 1 and not errors       # the mirror below the root is not a photo folder


def test_move_to_new_root_and_back(tmp_path):
    src = tmp_path / "G"
    event = make_event(src)
    cache = event / "CaptureOne" / "Cache"
    thumbs = cache_hashes(cache)
    old_root, new_root = tmp_path / "fast", tmp_path / "new ssd [512]"
    assert run("link", "--source", src, "--to", old_root, "--apply") == 0
    old_target = c.link_target(str(cache))
    assert run("move", "--source", src, "--to", new_root, "--apply") == 0
    new_target = c.link_target(str(cache))
    assert c._same(new_target, c.mirror(str(new_root), str(cache)))
    assert cache_hashes(new_target) == thumbs
    assert not os.path.exists(old_target)
    assert os.listdir(old_root) == [c.MAP_FILE_NAME]    # empty mirror folders pruned
    assert run("unlink", "--source", src, "--apply") == 0
    assert not c.is_link(cache) and cache.is_dir()
    assert cache_hashes(cache) == thumbs
    assert not os.path.exists(new_target)


def test_lost_fast_disk_is_repaired_empty(tmp_path):
    src = tmp_path / "G"
    event = make_event(src)
    cache = event / "CaptureOne" / "Cache"
    root = tmp_path / "fast"
    assert run("link", "--source", src, "--to", root, "--apply") == 0
    shutil.rmtree(root)                              # the SSD was replaced
    items, _ = c.scan([str(src)], quiet=True)
    assert items[0].state == "broken"
    assert run("move", "--source", src, "--to", root, "--apply") == 0
    assert c.is_link(cache) and os.path.isdir(c.link_target(str(cache)))
    other = tmp_path / "other"
    shutil.rmtree(root)
    assert run("move", "--source", src, "--to", other, "--apply") == 0
    assert c._same(c.link_target(str(cache)), c.mirror(str(other), str(cache)))
    assert os.listdir(c.link_target(str(cache))) == []


def test_unlink_discard_leaves_empty_real_folder(tmp_path):
    src = tmp_path / "G"
    event = make_event(src)
    cache = event / "CaptureOne" / "Cache"
    root = tmp_path / "fast"
    assert run("link", "--source", src, "--to", root, "--apply") == 0
    target = c.link_target(str(cache))
    assert run("unlink", "--source", src, "--discard", "--apply") == 0
    assert cache.is_dir() and not c.is_link(cache) and os.listdir(cache) == []
    assert not os.path.exists(target)


def test_failed_copy_rolls_back(tmp_path, monkeypatch):
    src = tmp_path / "G"
    event = make_event(src)
    cache = event / "CaptureOne" / "Cache"
    thumbs = cache_hashes(cache)

    def broken(a, b):
        raise OSError("disk full")
    monkeypatch.setattr(c, "copy_tree", broken)
    assert run("link", "--source", src, "--to", tmp_path / "fast", "--apply") == 1
    assert not c.is_link(cache) and cache_hashes(cache) == thumbs
    assert not os.path.exists(str(cache) + c.STAGING_SUFFIX)


def test_interrupted_link_is_recovered(tmp_path):
    src = tmp_path / "G"
    event = make_event(src)
    cache = event / "CaptureOne" / "Cache"
    thumbs = cache_hashes(cache)
    os.rename(cache, str(cache) + c.STAGING_SUFFIX)  # crash right after the rename
    root = tmp_path / "fast"
    assert run("link", "--source", src, "--to", root, "--apply") == 0
    assert c.is_link(cache) and cache_hashes(cache) == thumbs
    assert not os.path.exists(str(cache) + c.STAGING_SUFFIX)


def test_refuses_while_capture_one_runs(tmp_path, monkeypatch):
    src = tmp_path / "G"
    event = make_event(src)
    monkeypatch.setattr(c, "capture_one_running", lambda: "captureone.exe")
    assert run("link", "--source", src, "--to", tmp_path / "fast", "--apply") == 2
    assert not c.is_link(event / "CaptureOne" / "Cache")


def test_refuses_without_space(tmp_path, monkeypatch):
    src = tmp_path / "G"
    event = make_event(src)
    monkeypatch.setattr(c, "free_bytes", lambda path: 1000)
    assert run("link", "--source", src, "--to", tmp_path / "fast", "--apply") == 2
    assert not c.is_link(event / "CaptureOne" / "Cache")
    # --discard needs no space
    assert run("link", "--source", src, "--to", tmp_path / "fast", "--discard", "--apply") == 0


def test_long_path(tmp_path):
    src = tmp_path / "G"
    deep = src
    while len(str(deep)) < 280:
        deep = deep / "長いフォルダ名 [long] 長いフォルダ名"
    event = make_event(deep, photos=1)
    cache = event / "CaptureOne" / "Cache"
    assert len(str(cache)) > 260
    thumbs = cache_hashes(cache)
    assert run("link", "--source", src, "--to", tmp_path / "fast", "--apply") == 0
    assert c.is_link(cache) and cache_hashes(cache) == thumbs
    assert run("unlink", "--source", src, "--apply") == 0
    assert cache_hashes(cache) == thumbs


def test_never_deletes_a_non_cache_folder(tmp_path):
    victim = tmp_path / "Photos"
    victim.mkdir()
    with pytest.raises(OSError):
        c._clear_cache_folder(str(victim))
    assert victim.exists()


@pytest.mark.skipif(not os.path.isdir("E:\\temp_jxl"), reason="needs a second drive")
def test_junction_across_drives(tmp_path):
    src = tmp_path / "G"
    event = make_event(src)
    cache = event / "CaptureOne" / "Cache"
    thumbs = cache_hashes(cache)
    root = os.path.join("E:\\temp_jxl", f"c1link_test_{os.getpid()}_{tmp_path.name}")
    try:
        assert run("link", "--source", src, "--to", root, "--apply") == 0
        assert c.link_target(str(cache)).upper().startswith("E:")
        assert cache_hashes(cache) == thumbs
        assert run("unlink", "--source", src, "--apply") == 0
        assert cache_hashes(cache) == thumbs
    finally:
        shutil.rmtree(root, ignore_errors=True)
