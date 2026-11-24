import importlib.util
import tomllib
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

import vigie

ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pyproject(tmp_path: Path, version: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nname = "vigie"\nversion = "{version}"\n')
    return path


def test_tag_matching_the_package_version_is_accepted(tmp_path: Path) -> None:
    check = load_script("check_release_tag").check
    assert check("v1.2.3", pyproject(tmp_path, "1.2.3")) is None


@pytest.mark.parametrize(
    "tag", ["v1.2", "1.2.3", "v1.2.3-rc1", "v1.2.3.4", "main", "", "v1.2.3\nx"]
)
def test_malformed_tags_are_rejected(tmp_path: Path, tag: str) -> None:
    error = load_script("check_release_tag").check(tag, pyproject(tmp_path, "1.2.3"))
    assert error is not None
    assert "attendu vX.Y.Z" in error


def test_tag_not_matching_the_version_is_rejected(tmp_path: Path) -> None:
    error = load_script("check_release_tag").check("v1.2.4", pyproject(tmp_path, "1.2.3"))
    assert error is not None
    assert "v1.2.4" in error
    assert "1.2.3" in error


def test_main_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script("check_release_tag")
    monkeypatch.chdir(tmp_path)
    pyproject(tmp_path, "0.1.0")
    assert module.main(["x", "v0.1.0"]) == 0
    assert module.main(["x", "v0.2.0"]) == 1
    assert module.main(["x"]) == 2


def test_package_version_is_consistent_everywhere() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert vigie.__version__ == declared
    # le dépôt est prêt pour un premier tag : le script l'accepterait
    assert load_script("check_release_tag").check(f"v{declared}", ROOT / "pyproject.toml") is None


def make_wheel(directory: Path, names: list[str]) -> None:
    with zipfile.ZipFile(directory / "vigie-0.1.0-py3-none-any.whl", "w") as wheel:
        for name in names:
            wheel.writestr(name, "x")


def test_wheel_with_the_template_is_accepted(tmp_path: Path) -> None:
    make_wheel(tmp_path, ["vigie/templates/digest.html.j2", "vigie/py.typed", "vigie/radar/cli.py"])
    assert load_script("check_wheel").check(tmp_path) is None


def test_wheel_without_the_template_is_rejected(tmp_path: Path) -> None:
    make_wheel(tmp_path, ["vigie/py.typed", "vigie/radar/cli.py"])
    error = load_script("check_wheel").check(tmp_path)
    assert error is not None
    assert "vigie/templates/digest.html.j2" in error


def test_wheel_check_needs_exactly_one_wheel(tmp_path: Path) -> None:
    assert load_script("check_wheel").check(tmp_path) is not None
    make_wheel(tmp_path, ["vigie/templates/digest.html.j2", "vigie/py.typed", "vigie/radar/cli.py"])
    (tmp_path / "other-0.1.0-py3-none-any.whl").write_bytes(b"")
    assert load_script("check_wheel").check(tmp_path) is not None


def test_main_of_check_wheel(tmp_path: Path) -> None:
    module = load_script("check_wheel")
    assert module.main(["x"]) == 2
    assert module.main(["x", str(tmp_path)]) == 1
    make_wheel(tmp_path, ["vigie/templates/digest.html.j2", "vigie/py.typed", "vigie/radar/cli.py"])
    assert module.main(["x", str(tmp_path)]) == 0


def test_template_directory_resolution_prefers_the_installed_package(tmp_path: Path) -> None:
    from vigie.radar import digest

    assert (digest.TEMPLATE_DIR / "digest.html.j2").is_file()
    assert digest._template_dir() == digest.TEMPLATE_DIR


def test_the_wheel_force_includes_the_template_directory() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    include = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert include == {"templates": "vigie/templates"}
