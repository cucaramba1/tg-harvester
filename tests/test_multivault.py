"""Два vault'а: разделы, пути и запись находят нужный корень."""
from harvester.files import list_sections
from harvester.writer import WriteError, inside

import pytest

from .helpers import make_settings


def test_sections_paths_and_inside_span_both_vaults(tmp_path):
    second = tmp_path / "vault2"
    for root, sec in ((tmp_path / "vault", "ИИ работа"), (second, "Музыка")):
        (root / sec).mkdir(parents=True)
        (root / f"{sec}.canvas").write_text("{}")
    (second / "Музыка" / "Аккорды.md").write_text("x")
    s = make_settings(tmp_path, extra_vaults=(second,))
    assert list_sections(s.vaults) == ["ИИ работа", "Музыка"]
    assert s.path("Музыка/Аккорды.md") == second / "Музыка" / "Аккорды.md"
    assert s.root_of(second / "Музыка" / "Аккорды.md") == second
    assert inside(s.vaults, "Музыка/Аккорды.md") == second / "Музыка" / "Аккорды.md"
    with pytest.raises(WriteError):
        inside(s.vaults, "../x.md")
