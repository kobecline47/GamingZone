from pathlib import Path
import re

p = Path("Main1.py")
text = p.read_text(encoding="utf-8")
orig = text

bad_markers = "Ãâð"
pattern = re.compile(r"\S*[Ãâð]\S*")


def score(s: str) -> int:
    return sum(s.count(c) for c in bad_markers)


def maybe_fix(tok: str) -> str:
    best = tok
    best_score = score(tok)
    for enc in ("cp1252", "latin1"):
        try:
            cand = tok.encode(enc).decode("utf-8")
        except Exception:
            continue
        cand_score = score(cand)
        if cand_score < best_score:
            best = cand
            best_score = cand_score
    return best


changed = 0


def repl(m: re.Match[str]) -> str:
    global changed
    tok = m.group(0)
    fixed = maybe_fix(tok)
    if fixed != tok:
        changed += 1
    return fixed


text = pattern.sub(repl, text)

if text != orig:
    backup = p.with_suffix(p.suffix + ".bak-mojibake")
    backup.write_text(orig, encoding="utf-8")
    p.write_text(text, encoding="utf-8")
    print(f"Fixed tokens: {changed}")
    print(f"Backup: {backup}")
else:
    print("No mojibake token changes applied.")
