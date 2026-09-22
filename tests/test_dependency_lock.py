from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements-lock.txt"
WORKFLOW = ROOT / ".github" / "workflows" / "build-windows.yml"


def _requirement_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        if line[:1].isspace():
            if not current:
                raise AssertionError("orphan continuation in requirements lock")
            current.append(stripped)
            continue
        if current:
            blocks.append("\n".join(current))
        current = [stripped]
    if current:
        blocks.append("\n".join(current))
    return blocks


def test_every_locked_requirement_has_exact_sha256_hash():
    blocks = _requirement_blocks(LOCK.read_text(encoding="utf-8"))

    assert blocks
    for block in blocks:
        first = block.splitlines()[0]
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==[^\\\s]+ \\", first)
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", block)
        assert len(hashes) == 1, block


def test_windows_ci_enforces_hashes_and_has_no_discovery_step():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert (
        "pip install --require-hashes --no-deps -r requirements-lock.txt"
        in workflow
    )
    assert "Report exact Windows wheel hashes" not in workflow
    assert "LOCK_HASH " not in workflow
