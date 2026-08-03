"""주석 규칙 검사기 (`CLAUDE.md` 주석 규칙).

규칙마다 위반 위치를 `파일:줄` 로 보고하고, 하나라도 있으면 1 로 끝난다.
표준 라이브러리만 쓴다 — 린트 의존성 없이 컨테이너 밖에서도 돌아야 한다.
"""

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

MODULE_DOC_MAX = 5
FUNCTION_DOC_MAX = 3
INLINE_RUN_MAX = 2

# 선언조·수사적 문장의 표지. "왜"를 적은 주석은 근거를 대지 단정하지 않는다.
RHETORIC = re.compile(
    r"(안 된다|해서는 안|반드시|절대|결코|~해야만|바로 그|다름 아닌|말이 아니라"
    r"|이것이 전부|핵심이다|요점이다|중요하다|명심)"
)


def analyze(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    problems: list[str] = []

    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        count = first.end_lineno - first.lineno + 1
        doc_lines.update(range(first.lineno, first.end_lineno + 1))
        limit = MODULE_DOC_MAX if isinstance(node, ast.Module) else FUNCTION_DOC_MAX
        label = "모듈" if isinstance(node, ast.Module) else getattr(node, "name", "?")
        if count > limit:
            problems.append(
                f"{path}:{first.lineno} docstring {count}줄 (상한 {limit}) — {label}"
            )

    own_line: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and lines[token.start[0] - 1].lstrip().startswith("#"):
            own_line.add(token.start[0])

    start = run = 0
    for number in range(1, len(lines) + 2):
        if number in own_line:
            start = start or number
            run += 1
            continue
        if run > INLINE_RUN_MAX:
            problems.append(f"{path}:{start} 인라인 주석 {run}줄 (상한 {INLINE_RUN_MAX})")
        start = run = 0

    comment_lines = doc_lines | own_line
    for number in sorted(comment_lines):
        text = lines[number - 1]
        if "**" in text:
            problems.append(f"{path}:{number} 강조 마크업")
        found = RHETORIC.search(text)
        if found:
            problems.append(f"{path}:{number} 선언조·수사 ({found.group()})")

    code = sum(
        1 for i, text in enumerate(lines, 1) if text.strip() and i not in comment_lines
    )
    if len(comment_lines) > code:
        problems.append(f"{path}:1 주석 {len(comment_lines)}줄 > 코드 {code}줄")
    return problems


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv[1:]] or [Path("src"), Path("tests"), Path("scripts")]
    targets = sorted(
        p
        for root in roots
        for p in ([root] if root.is_file() else root.rglob("*.py"))
        if "__pycache__" not in p.parts
    )
    problems = [line for path in targets for line in analyze(path)]
    for line in problems:
        print(line)
    print(f"\n{len(targets)}개 파일, 위반 {len(problems)}건")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
