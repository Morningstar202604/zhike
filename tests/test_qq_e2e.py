from scripts import qq_e2e


def test_qq_e2e_runtime_dir_stays_in_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(qq_e2e, "ROOT", tmp_path)
    runtime_dir = qq_e2e._new_runtime_dir()
    assert runtime_dir.is_dir()
    assert runtime_dir.parent == tmp_path / ".tmp" / "qq_e2e"
