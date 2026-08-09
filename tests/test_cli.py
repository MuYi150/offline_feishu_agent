from __future__ import annotations

import json
from pathlib import Path

from wiki_review_v2.cli import main


def test_list_cases(capsys) -> None:
    assert main(["--list-cases"]) == 0
    assert "basic_pass" in capsys.readouterr().out


def test_cli_requires_action(capsys) -> None:
    assert main([]) == 2
    assert "error" in capsys.readouterr().out


def test_similarity_index_cli_actions(tmp_path, monkeypatch, capsys) -> None:
    index_path = tmp_path / "similarity" / "articles.sqlite"
    monkeypatch.setenv("SIMILARITY_INDEX_PATH", str(index_path))
    assert main(["--init-similarity-index"]) == 0
    assert '"record_count": 0' in capsys.readouterr().out
    assert main(["--similarity-index-info"]) == 0
    output = capsys.readouterr().out
    assert '"schema_version": "1"' in output
    assert Path(json.loads(output)["path"]) == index_path

