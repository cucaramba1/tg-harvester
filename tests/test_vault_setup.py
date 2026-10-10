import json

from harvester.vault_setup import ROOT_CANVAS, ROOT_GROUP, ensure_section


def _root(vault):
    nodes = [{"id": "g", "type": "group", "label": ROOT_GROUP, "x": 0, "y": 1080, "width": 2400, "height": 460}]
    nodes += [{"id": f"n{i}", "type": "file", "file": f"L{i}.canvas", "x": 30 + (i % 5) * 460,
               "y": 1140 + (i // 5) * 200, "width": 440, "height": 180} for i in range(10)]
    (vault / ROOT_CANVAS).write_text(json.dumps({"nodes": nodes, "edges": []}), "utf-8")


def test_ensure_section_creates_folder_moc_canvas_and_root_link(tmp_path):
    _root(tmp_path)
    created = ensure_section(tmp_path, "Музыка")
    assert created == ["Музыка/Музыка — карта заметок.md", "Музыка.canvas", ROOT_CANVAS]
    assert (tmp_path / "Музыка" / "Музыка — карта заметок.md").read_text("utf-8") == "# Музыка — карта заметок\n"
    cv = json.loads((tmp_path / "Музыка.canvas").read_text("utf-8"))
    assert [n["file"] for n in cv["nodes"]] == ["Музыка/Музыка — карта заметок.md"]
    root = json.loads((tmp_path / ROOT_CANVAS).read_text("utf-8"))
    n = next(x for x in root["nodes"] if x.get("file") == "Музыка.canvas")
    assert (n["x"], n["y"], n["width"], n["height"]) == (30, 1540, 440, 180)
    g = next(x for x in root["nodes"] if x["id"] == "g")
    assert g["height"] == 1540 + 180 + 40 - 1080


def test_ensure_section_is_idempotent_and_works_without_root(tmp_path):
    assert ensure_section(tmp_path, "Музыка") == ["Музыка/Музыка — карта заметок.md", "Музыка.canvas"]
    assert ensure_section(tmp_path, "Музыка") == []
    _root(tmp_path)
    assert ensure_section(tmp_path, "Музыка") == [ROOT_CANVAS]
    assert ensure_section(tmp_path, "Музыка") == []
