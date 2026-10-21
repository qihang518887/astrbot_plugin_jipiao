"""Build an installable source archive without environments or user databases."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    root = Path(__file__).resolve().parent
    target = root / "dist" / "astrbot_plugin_jipiao.zip"
    target.parent.mkdir(exist_ok=True)
    fli_files = [
        p
        for p in (root / "fli").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc")
    ]
    files = [
        *root.glob("*.py"),
        root / "metadata.yaml",
        root / "_conf_schema.json",
        root / "requirements.txt",
        root / "README.md",
        root / "pyproject.toml",
        *fli_files,
        *root.glob("tests/*.py"),
        *root.glob("validation/*.json"),
    ]
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, Path(root.name) / path.relative_to(root))
    print(target)


if __name__ == "__main__":
    main()
