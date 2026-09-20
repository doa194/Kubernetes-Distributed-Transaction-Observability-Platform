"""Image tags are content hashes: deployments only restart pods when the source really changed."""

import pytest

from txplatform import images, tools


def _repo(tmp_path, order_source: str = "class Order;"):
    for name in ("global.json", "Directory.Build.props", "Directory.Packages.props", ".dockerignore"):
        (tmp_path / name).write_text("shared", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Dockerfile").write_text("FROM sdk", encoding="utf-8")
    for project, source in (("ServiceDefaults", "class Defaults;"), ("OrderService", order_source), ("FraudService", "class Fraud;")):
        folder = tmp_path / "src" / project
        folder.mkdir()
        (folder / f"{project}.cs").write_text(source, encoding="utf-8")
    return tmp_path


def test_the_same_source_always_produces_the_same_tag(tmp_path):
    root = _repo(tmp_path)

    assert images.content_tag("OrderService", root) == images.content_tag("OrderService", root)


def test_a_changed_source_produces_a_different_tag(tmp_path):
    root = _repo(tmp_path)
    before = images.content_tag("OrderService", root)

    (root / "src" / "OrderService" / "OrderService.cs").write_text("class Order; // changed", encoding="utf-8")

    assert images.content_tag("OrderService", root) != before


def test_a_changed_shared_project_changes_every_service(tmp_path):
    root = _repo(tmp_path)
    before = {p: images.content_tag(p, root) for p in ("OrderService", "FraudService")}

    (root / "src" / "ServiceDefaults" / "ServiceDefaults.cs").write_text("class Defaults; // changed", encoding="utf-8")

    assert all(images.content_tag(p, root) != tag for p, tag in before.items())


def test_different_services_do_not_share_a_tag(tmp_path):
    root = _repo(tmp_path)

    assert images.content_tag("OrderService", root) != images.content_tag("FraudService", root)


def test_build_output_directories_are_ignored(tmp_path):
    root = _repo(tmp_path)
    before = images.content_tag("OrderService", root)
    build_output = root / "src" / "OrderService" / "obj"
    build_output.mkdir()
    (build_output / "generated.cs").write_text("generated", encoding="utf-8")

    assert images.content_tag("OrderService", root) == before


def test_an_unknown_project_is_refused_instead_of_hashing_nothing(tmp_path):
    root = _repo(tmp_path)

    with pytest.raises(tools.ToolError, match="no .NET project folder"):
        images.content_tag("order-service", root)
