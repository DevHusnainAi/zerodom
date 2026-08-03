import pytest

from zerodom import cli

PAGE = """
<html><title>Shop</title><body>
  <a href="/cart">View cart</a>
  <button>Complete checkout</button>
  <input name="q" aria-label="Search products">
</body></html>
"""


@pytest.fixture
def served(monkeypatch):
    """Skip the network: the CLI's only job here is what it does with the HTML."""
    monkeypatch.setattr(cli, "fetch", lambda url, render: (PAGE, url))


def test_compact_is_the_default_output(served, capsys):
    cli.main(["https://shop.test/"])
    out = capsys.readouterr().out
    assert "[02] button 'Complete checkout'" in out
    assert '"selector"' not in out, "JSON must be opt-in"
    assert "compact text" in out


def test_json_flag_emits_the_full_graph(served, capsys):
    cli.main(["https://shop.test/", "--json"])
    out = capsys.readouterr().out
    assert '"selector":' in out and "(json)" in out


def test_find_prints_only_matching_nodes(served, capsys):
    cli.main(["https://shop.test/", "--find", "checkout"])
    out = capsys.readouterr().out
    assert "[02] button 'Complete checkout'" in out
    assert "View cart" not in out


def test_find_matches_every_term(served, capsys):
    cli.main(["https://shop.test/", "--find", "input search"])
    out = capsys.readouterr().out
    assert "[03] input 'Search products'" in out
    assert "button" not in out.split("─")[0]


def test_find_reports_a_miss_without_dumping_the_graph(served, capsys):
    cli.main(["https://shop.test/", "--find", "delete account"])
    out = capsys.readouterr().out
    assert "No node matches 'delete account' among 3 nodes." in out
    assert "View cart" not in out


def test_the_inspect_verb_is_optional(served, capsys):
    """`inspect` is the only verb, so requiring it is ceremony — but it must still work."""
    cli.main(["https://shop.test/"])
    bare = capsys.readouterr().out
    cli.main(["inspect", "https://shop.test/"])
    verbose = capsys.readouterr().out
    # Everything but the summary, whose measured latency differs run to run.
    assert verbose.split("─")[0] == bare.split("─")[0]


def test_a_local_path_is_accepted_as_a_target(tmp_path, capsys):
    page = tmp_path / "page.html"
    page.write_text(PAGE, encoding="utf-8")
    cli.main([str(page)])
    out = capsys.readouterr().out
    assert "[01] a 'View cart'" in out
    assert page.as_uri() in out
