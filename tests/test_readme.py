from pathlib import Path


def test_readme_preserves_attribution_without_local_snapshot_links():
    readme = Path(__file__).resolve().parents[1] / "README.md"
    content = readme.read_text(encoding="utf-8")

    assert "https://github.com/drkostas/hevy2garmin" in content
    assert "Konstantinos Georgiou" in content
    assert "https://www.liftosaur.com/doc/api" in content
    assert "liftosaur/docs/content/api.md" not in content
