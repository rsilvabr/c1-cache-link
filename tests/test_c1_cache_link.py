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
    # a real rules file next to the script must never leak into the tests
    monkeypatch.setattr(c, "RULES_FILE", str(tmp_path / "no_rules_here.toml"))


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


# --- rules: resolver (pure, no disk) -------------------------------------------

def rules_of(default="D:\\Default", paths=(), names=()):
    return c.Rules(default, list(paths), list(names))


def c1(*folders):
    return os.path.join(*folders, "CaptureOne")


def test_resolve_default_when_no_rule():
    assert c.resolve(rules_of(), c1("G:\\", "2025", "Ev")) == ("D:\\Default", "default")
    assert c.resolve(rules_of(default=None), c1("G:\\", "2025", "Ev")) == (None, "default")


def test_name_rule_deepest_match_wins():
    r = rules_of(names=[("OUTTAKES", None), ("Selects", "E:\\Sel")])
    assert c.resolve(r, c1("G:\\", "Selects", "Ev")) == ("E:\\Sel", "name Selects")
    assert c.resolve(r, c1("G:\\", "OUTTAKES", "Ev")) == (None, "name OUTTAKES")
    assert c.resolve(r, c1("G:\\", "Selects", "OUTTAKES", "Ev")) == (None, "name OUTTAKES")
    assert c.resolve(r, c1("G:\\", "OUTTAKES", "Selects", "Ev")) == ("E:\\Sel", "name Selects")
    assert c.resolve(r, c1("G:\\", "2025", "Ev", "OUTTAKES")) == (None, "name OUTTAKES")
    assert c.resolve(r, c1("G:\\", "2025", "Ev")) == ("D:\\Default", "default")


def test_name_rule_is_case_insensitive():
    r = rules_of(names=[("OUTTAKES", None)])
    assert c.resolve(r, c1("g:\\", "2025", "outtakes")) == (None, "name OUTTAKES")


def test_name_rule_beats_path_rule():
    r = rules_of(paths=[("G:\\2018", "F:\\C1")], names=[("OUTTAKES", None)])
    assert c.resolve(r, c1("G:\\2018", "Ev", "OUTTAKES")) == (None, "name OUTTAKES")
    assert c.resolve(r, c1("G:\\2018", "Ev")) == ("F:\\C1", "path G:\\2018")


@pytest.mark.parametrize("order", [0, 1])
def test_longest_path_wins_whatever_the_order(order):
    rules = [("G:\\2018", "F:\\C1"), ("G:\\2018\\Sub", "E:\\Fast")]
    if order:
        rules.reverse()
    r = rules_of(paths=rules)
    assert c.resolve(r, c1("G:\\2018", "Sub", "Ev")) == ("E:\\Fast", "path G:\\2018\\Sub")
    assert c.resolve(r, c1("G:\\2018", "Other", "Ev")) == ("F:\\C1", "path G:\\2018")


def test_path_prefix_is_whole_folder_names():
    r = rules_of(paths=[("G:\\2018", "F:\\C1")])
    assert c.resolve(r, c1("G:\\20180", "Ev")) == ("D:\\Default", "default")
    assert c.resolve(r, c1("G:\\2018", "Ev")) == ("F:\\C1", "path G:\\2018")
    assert c.resolve(r, c1("g:\\2018\\", "Ev")) == ("F:\\C1", "path G:\\2018")


def test_root_source_matches_everything_on_its_drive():
    r = rules_of(paths=[("G:\\", "F:\\C1")])
    assert c.resolve(r, c1("G:\\", "2025", "Ev")) == ("F:\\C1", "path G:\\")
    assert c.resolve(r, c1("E:\\", "2025", "Ev")) == ("D:\\Default", "default")


def test_hostile_names_in_paths_and_names():
    src = f"G:\\{HOSTILE}"
    r = rules_of(paths=[(src, "F:\\C1 [x]")], names=[("FINAL [x] 50%", None)])
    assert c.resolve(r, c1(src, "(2) 吾妻 [a]")) == ("F:\\C1 [x]", f"path {src}")
    assert c.resolve(r, c1(src, "2025", "FINAL [x] 50%")) == (None, "name FINAL [x] 50%")


# --- rules: loading and validation ---------------------------------------------

VALID_RULES = """
default = 'E:\\C1_Cache'
[[path]]
source = 'G:\\2018'
to = 'F:\\C1_Cache'
[[path]]
source = 'G:\\2018\\Sub'
to = 'none'
[[name]]
name = "OUTTAKES"
to = "NONE"
"""


def write_text(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def lit(value):
    return "'none'" if value == "none" else f"'{value}'"


def write_rules(path, default, paths=(), names=()):
    lines = [f"default = {lit(default)}"]
    for source, to in paths:
        lines += ["[[path]]", f"source = '{source}'", f"to = {lit(to)}"]
    for name, to in names:
        lines += ["[[name]]", f'name = "{name}"', f"to = {lit(to)}"]
    return write_text(path, "\n".join(lines) + "\n")


def test_load_rules_valid(tmp_path):
    rules = c.load_rules(write_text(tmp_path / "r.toml", VALID_RULES), "X:\\fallback")
    assert rules.default == "E:\\C1_Cache"
    assert rules.paths == [("G:\\2018", "F:\\C1_Cache"), ("G:\\2018\\Sub", None)]
    assert rules.names == [("OUTTAKES", None)]


def test_load_rules_default_falls_back(tmp_path):
    rules = c.load_rules(write_text(tmp_path / "r.toml", "[[name]]\nname = 'X'\nto = 'none'\n"),
                         "X:\\fallback")
    assert rules.default == "X:\\fallback"


@pytest.mark.parametrize("text, fragment", [
    ("bogus = 1\n", "unknown key(s) bogus"),
    ("[[path]]\nsource = 'G:\\\\2018'\nto = 'F:\\\\C1'\nextra = 1\n", "unknown key(s) extra"),
    ("[[name]]\nname = 'A'\nto = 'none'\nwhere = 'x'\n", "unknown key(s) where"),
    ("default = 'C1_Cache'\n", "not an absolute folder"),
    ("default = '\\C1_Cache'\n", "not an absolute folder"),
    ("[[path]]\nsource = 'G:\\\\2018'\nto = 'relative'\n", "not an absolute folder"),
    ("[[path]]\nsource = 'none'\nto = 'F:\\\\C1'\n", "not an absolute folder"),
    ("[[path]]\nsource = 'G:\\\\2018'\n", "missing 'to'"),
    ("[[name]]\nname = ''\nto = 'none'\n", "must be a folder name"),
    ("[[name]]\nname = 'OUT/TAKES'\nto = 'none'\n", "must be a folder name"),
    ("[[name]]\nname = 'OUT:TAKES'\nto = 'none'\n", "must be a folder name"),
    ("[[name]]\nname = 'A'\nto = 'none'\n[[name]]\nname = 'a'\nto = 'none'\n", "already used"),
    ("[[path]]\nsource = 'G:\\\\2018'\nto = 'none'\n"
     "[[path]]\nsource = 'g:\\\\2018\\\\'\nto = 'none'\n", "already used"),
    ("default = \n", "not valid TOML"),
    ("path = 'x'\n", "must be a list"),
])
def test_load_rules_reports_each_problem(tmp_path, text, fragment):
    path = write_text(tmp_path / "bad.toml", text)
    with pytest.raises(ValueError) as err:
        c.load_rules(path, "X:\\fallback")
    assert fragment in str(err.value)
    assert path in str(err.value)                    # the message names the file


def test_bad_rules_file_exits_2_without_traceback(tmp_path, capsys):
    src = tmp_path / "G"
    make_event(src)
    bad = write_text(tmp_path / "bad.toml", "default = \n")
    assert run("status", "--source", src, "--rules", bad) == 2
    assert "Rules file error" in capsys.readouterr().out
    assert run("link", "--source", src, "--rules", tmp_path / "missing.toml") == 2


# --- rules: space per drive ----------------------------------------------------

def fake_item(state, cache, nbytes, target=""):
    return c.Item(os.path.dirname(cache), cache, state=state, target=target, nbytes=nbytes)


def test_space_needs_are_counted_per_drive():
    todo = [
        (fake_item("real", "G:\\a\\CaptureOne\\Cache", 100), "link", "E:\\C1"),
        (fake_item("linked", "G:\\b\\CaptureOne\\Cache", 50, "E:\\C1\\G\\b\\CaptureOne\\Cache"),
         "unlink", None),
        (fake_item("linked", "G:\\c\\CaptureOne\\Cache", 70, "F:\\old\\G\\c\\CaptureOne\\Cache"),
         "move", "F:\\new"),
        (fake_item("broken", "G:\\d\\CaptureOne\\Cache", 0, "F:\\gone\\G\\d\\CaptureOne\\Cache"),
         "repair", "E:\\C1"),
    ]
    assert c.space_needs(todo, discard=False) == {"e:": 100, "g:": 50, "f:": 70}
    assert c.space_needs(todo, discard=True) == {}


def test_check_space_refuses_when_one_drive_is_short(monkeypatch, capsys):
    monkeypatch.setattr(c, "free_bytes", lambda p: 10 ** 12 if p.lower().startswith("f") else 1000)
    assert c.check_space({"e:": 1, "f:": 1}) is False
    out = capsys.readouterr().out
    assert "Space: E:" in out and "DOES NOT FIT" in out and "Space: F:" in out
    assert c.check_space({"f:": 1}) is True


# --- rules: end to end on tmp_path ---------------------------------------------

def points_to(cache, root):
    return c.is_link(cache) and c._same(c.link_target(str(cache)), c.mirror(str(root), str(cache)))


def test_rules_route_each_cache_and_follow_changes(tmp_path, capsys):
    src = tmp_path / "G"
    A, B = tmp_path / "rootA", tmp_path / "rootB"
    old_event = make_event(src / "2018", "180101 Old")
    hostile = make_event(src / "2025")
    outtakes = make_event(hostile, "OUTTAKES", photos=2)     # 2025/<hostile>/OUTTAKES
    caches = {"old": old_event / "CaptureOne" / "Cache",
              "hostile": hostile / "CaptureOne" / "Cache",
              "outtakes": outtakes / "CaptureOne" / "Cache"}
    hashes = {k: cache_hashes(v) for k, v in caches.items()}
    settings = snapshot(src)
    twenty25 = str(src / "2025")

    rules = write_rules(tmp_path / "rules.toml", str(A), paths=[(twenty25, str(B))],
                        names=[("OUTTAKES", "none")])
    # dry run: nothing changes, no root is created
    assert run("link", "--source", src, "--rules", rules) == 0
    assert snapshot(src) == settings
    assert not A.exists() and not B.exists()
    assert "PLAN" in capsys.readouterr().out

    # apply: each cache where its rule says; OUTTAKES stays beside the photos
    assert run("link", "--source", src, "--rules", rules, "--apply") == 0
    assert points_to(caches["old"], A)
    assert points_to(caches["hostile"], B)
    assert not c.is_link(caches["outtakes"]) and caches["outtakes"].is_dir()
    assert snapshot(src) == settings
    for key, cache in caches.items():
        assert cache_hashes(cache) == hashes[key]
    text = (A / c.MAP_FILE_NAME).read_text(encoding="utf-8")
    assert "\tname OUTTAKES" in text and f"\tpath {twenty25}" in text

    # rules changed: sub-folder -> A, OUTTAKES -> B
    rules = write_rules(tmp_path / "rules.toml", str(A), paths=[(twenty25, str(A))],
                        names=[("OUTTAKES", str(B))])
    assert run("link", "--source", src, "--rules", rules, "--apply") == 0
    assert points_to(caches["hostile"], A) and points_to(caches["old"], A)
    assert points_to(caches["outtakes"], B)
    assert not os.path.exists(c.mirror(str(B), str(caches["hostile"])))   # old copy gone
    for key, cache in caches.items():
        assert cache_hashes(cache) == hashes[key]

    # a rule to "none": the cache comes back beside the photos
    rules = write_rules(tmp_path / "rules.toml", str(A), paths=[(twenty25, "none")],
                        names=[("OUTTAKES", str(B))])
    assert run("link", "--source", src, "--rules", rules, "--apply") == 0
    assert caches["hostile"].is_dir() and not c.is_link(caches["hostile"])
    assert not os.path.exists(c.mirror(str(A), str(caches["hostile"])))
    for key, cache in caches.items():
        assert cache_hashes(cache) == hashes[key]
    assert snapshot(src) == settings

    # and a second run has nothing to do
    capsys.readouterr()
    assert run("link", "--source", src, "--rules", rules, "--apply") == 0
    assert "Nothing to do." in capsys.readouterr().out


def test_status_prints_plan_and_changes_nothing(tmp_path, capsys):
    src = tmp_path / "G"
    make_event(src / "2018")
    rules = write_rules(tmp_path / "rules.toml", str(tmp_path / "rootA"))
    before = snapshot(src, skip_cache=False)
    assert run("status", "--source", src, "--rules", rules) == 0
    out = capsys.readouterr().out
    assert "PLAN" in out and "in place" in out
    assert snapshot(src, skip_cache=False) == before
    assert not (tmp_path / "rootA").exists()


def test_move_with_rules_file_prints_the_note(tmp_path, capsys):
    src = tmp_path / "G"
    make_event(src)
    rules = write_rules(tmp_path / "rules.toml", str(tmp_path / "rootA"))
    assert run("move", "--source", src, "--to", tmp_path / "rootB", "--rules", rules) == 0
    assert "a rules file is in use" in capsys.readouterr().out


def test_rules_space_refusal_changes_nothing(tmp_path, monkeypatch):
    src = tmp_path / "G"
    event = make_event(src)
    rules = write_rules(tmp_path / "rules.toml", str(tmp_path / "rootA"))
    monkeypatch.setattr(c, "free_bytes", lambda path: 1000)
    assert run("link", "--source", src, "--rules", rules, "--apply") == 2
    assert not c.is_link(event / "CaptureOne" / "Cache")
    assert run("link", "--source", src, "--rules", rules, "--discard", "--apply") == 0
