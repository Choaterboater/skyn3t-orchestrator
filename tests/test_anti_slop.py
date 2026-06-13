from skyn3t.agents import anti_slop


def test_flags_placeholder_and_em_dash():
    text = "<div>Jane Doe</div><p>one — two — three</p>"
    rules = {f["rule"] for f in anti_slop.scan_text(text, "src/App.jsx")}
    assert "placeholder_content" in rules
    assert "em_dash_copy" in rules


def test_ignores_non_markup_files():
    assert anti_slop.scan_text("Jane Doe and an Acme Inc", "data.json") == []


def test_flags_banned_font_and_scroll():
    text = "body { font-family: 'Fraunces'; } addEventListener('scroll', onScroll)"
    rules = {f["rule"] for f in anti_slop.scan_text(text, "styles.css")}
    assert "banned_font" in rules
    assert "scroll_listener" in rules


def test_scan_project_skips_node_modules(tmp_path):
    (tmp_path / "App.jsx").write_text("const c = 'Acme Inc'; addEventListener('scroll', f)")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "junk.jsx").write_text("Jane Doe")
    out = anti_slop.scan_project(tmp_path)
    rules = {f["rule"] for f in out}
    assert "placeholder_content" in rules and "scroll_listener" in rules
    assert all("node_modules" not in f["path"] for f in out)
