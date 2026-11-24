"""Build a distribution ZIP from source files, excluding all local run data."""

from pathlib import Path
import tempfile
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
RELEASE_FILES = (
    ".python-version",
    ".gitignore",
    ".agents/skills/geo-collector/SKILL.md",
    ".agents/skills/geo-collector/references/install.md",
    "AGENTS.md",
    "FLOW.md",
    "INSTALL.md",
    "README.md",
    "install-windows.cmd",
    "pyproject.toml",
    "uv.lock",
)


def build_release():
    with (ROOT / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    folder = f"geo-collector-{version}"
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    target = dist / f"{folder}-setup.zip"
    sources = sorted((ROOT / "src/geo_collector").rglob("*.py"))
    with tempfile.TemporaryDirectory(dir=dist) as temporary:
        archive_path = Path(temporary) / target.name
        with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
            for path in (*(ROOT / name for name in RELEASE_FILES), *sources):
                if path.resolve() != path or not path.is_file():
                    raise ValueError(f"Expected a regular project file: {path}")
                member = f"{folder}/{path.relative_to(ROOT).as_posix()}"
                if path.suffix == ".cmd":
                    text = path.read_text(encoding="ascii").replace("\n", "\r\n")
                    archive.writestr(member, text.encode("ascii"))
                else:
                    archive.write(path, member)
        archive_path.replace(target)
    return target


if __name__ == "__main__":
    print(build_release())
