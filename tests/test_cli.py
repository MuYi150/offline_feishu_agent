from __future__ import annotations

from wiki_review_v2.cli import main


def test_list_cases(capsys) -> None:
    assert main(["--list-cases"]) == 0
    assert "basic_pass" in capsys.readouterr().out


def test_cli_requires_action(capsys) -> None:
    assert main([]) == 2
    assert "error" in capsys.readouterr().out

