"""Prepare a clean PyPI + GitHub Release tag safely from the project root.

Updates pyproject.toml version, refreshes uv.lock, verifies compileall/pytest/build,
and prints (or optionally executes with --tag/--push) the git release commands.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$")


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=ROOT, check=check)


def current_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def bump_version(new_version: str) -> None:
    path = ROOT / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'^(version\s*=\s*")([^"]+)(")',
        rf'\g<1>{new_version}\g<3>',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise RuntimeError("Could not locate project version in pyproject.toml")
    path.write_text(updated, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare and verify a Geo Collector release.")
    parser.add_argument("version", help="New version number, e.g. 0.1.3 or 0.2.0rc1")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print plan without editing files")
    parser.add_argument("--skip-tests", action="store_true", help="Skip pytest (CI still runs full tests on tag push)")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow running with uncommitted changes")
    parser.add_argument("--tag", action="store_true", help="Create git commit and annotated tag v<version>")
    parser.add_argument("--push", action="store_true", help="Push commit and tag v<version> to origin (implies --tag)")
    args = parser.parse_args(argv)

    if not SEMVER.fullmatch(args.version):
        raise SystemExit(f"Invalid PEP 440 version: {args.version}")

    old = current_version()
    tag = f"v{args.version}"
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
    if status and not args.allow_dirty and not args.dry_run:
        raise SystemExit("Working tree is dirty. Commit or stash changes first (or pass --allow-dirty).")

    existing_tags = subprocess.check_output(["git", "tag", "--list", tag], cwd=ROOT, text=True).strip()
    if existing_tags:
        raise SystemExit(f"Git tag {tag} already exists.")

    print(f"Release plan: {old} -> {args.version} (tag: {tag})")
    if args.dry_run:
        return 0

    if args.version != old:
        bump_version(args.version)
        run(["uv", "lock"])

    run(["uv", "run", "python", "-m", "compileall", "-q", "src"])
    run(["git", "diff", "--check"])
    if not args.skip_tests:
        run(["uv", "run", "pytest", "-q"])
    run(["uv", "build", "--no-sources", "--out-dir", "dist"])
    run(["uv", "run", "python", "scripts/build_release.py"])

    if args.tag or args.push:
        run(["git", "add", "pyproject.toml", "uv.lock"])
        run(["git", "commit", "-m", f"release: {tag}"])
        run(["git", "tag", "-a", tag, "-m", f"Release {tag}"])
        if args.push:
            run(["git", "push", "origin", "HEAD", "--follow-tags"])
            print(f"Pushed {tag}. GitHub Actions will now verify, publish to PyPI, and create the GitHub Release.")
            return 0

    print("\nLocal release verification succeeded. Next steps:")
    print(f"  git add pyproject.toml uv.lock && git commit -m 'release: {tag}'")
    print(f"  git tag -a {tag} -m 'Release {tag}'")
    print("  git push origin HEAD --follow-tags")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
