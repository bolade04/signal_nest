#!/usr/bin/env python3
"""Bounded shell command extraction (Gate 4N-I28S, RC-S1 / RC-S2 / RC-S3).

THE DEFECT THIS CLOSES. Gate 4N-I28Q rejected a candidate 4 FAIL / 2 PASS because
``site_taxonomy.release_roots`` ended in a regex over the whole text of ci.yml: any
``scripts/<name>.py`` substring anywhere in the file — inside a COMMENT — became an executable
command root with a synthetic step id and an empty argv. Two behaviour-preserving, comment-only
edits moved the derived universe while every control stayed green and CI behaviour stayed
byte-identical:

    delete the mention of smoke_http.py from a comment    457 -> 454 sites, clean=True
    add "(see scripts/enforcement_path.py)" to a comment  457 -> 465 sites, clean=True

Root membership was a property of PROSE. The real invocation it was accidentally standing in for
is a shell indirection the model never looked at::

    ci.yml step "HTTP isolation smoke test"  ->  bash scripts/ci-smoke.sh  ->  ci-smoke.sh:63

Deleting the regex alone was explicitly prohibited and would have been strictly worse: smoke_http.py
would have left the universe entirely, silently dropping a control that checks unauthenticated
access is refused and that no opportunity id appears under two locations.

WHAT THIS MODULE DOES. It reads a shell script as SYNTAX and reports, for every command position,
what is actually executed. It never runs anything: executing a script to find out what it executes
is both unsafe and circular.

THE THREE OUTCOMES, and why there are three. A textual occurrence of a script path is:

    EXECUTABLE_INVOCATION   a command position proves it runs
    NONEXECUTABLE_MENTION   syntax proves it cannot run here (comment, heredoc data, echo argument,
                            an assignment never used as a command)
    UNRESOLVED_MENTION      neither could be proven

Two outcomes would force every unproven case into one of the certain ones, and both directions are
wrong: calling it executable re-creates the I28Q defect in a new place, and calling it inert hides a
real control. UNRESOLVED is a first-class answer that FAILS CLOSED — the caller surfaces it as a
problem rather than silently choosing.

WHAT IS DELIBERATELY NOT SUPPORTED. `eval`, dynamic command construction, unresolvable interpreter
variables, and loops over non-literal vectors all become UNRESOLVED rather than a guess. A model
that guesses is the thing being replaced.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

EXECUTABLE_INVOCATION = "EXECUTABLE_INVOCATION"
NONEXECUTABLE_MENTION = "NONEXECUTABLE_MENTION"
UNRESOLVED_MENTION = "UNRESOLVED_MENTION"

# Names that mean "a Python interpreter" when they appear in command position. Matched on the
# BASENAME of a resolved word, so "$VENV_DIR/bin/python" qualifies through its literal tail even
# though the directory is unknown.
PYTHON_BASENAMES = ("python", "python3", "python3.11", "python3.12", "python3.13")

# Commands that consume their arguments as DATA. A script path here is printed or copied, never run.
DATA_CONSUMERS = frozenset({
    "echo", "printf", "cat", "print", ":", "true", "false", "test", "[",
    "grep", "egrep", "fgrep", "rg", "sed", "awk", "cut", "sort", "uniq", "wc", "head", "tail",
    "ls", "stat", "file", "dirname", "basename", "readlink", "realpath",
    "cp", "mv", "rm", "mkdir", "touch", "chmod", "chown", "ln", "tar", "zip", "unzip",
    "shasum", "sha256sum", "md5sum", "diff", "comm", "tee", "xargs",
})

# Words that introduce a command position after them rather than being the command themselves.
COMMAND_PREFIXES = frozenset({"exec", "command", "time", "nohup", "nice", "builtin", "eval"})

SHELL_LAUNCHERS = frozenset({"bash", "sh", "zsh", "ksh", "dash"})

# Operators that end one command and begin another.
_SPLIT = re.compile(r"\|\||&&|;;|;|\||&(?!&)")

_KEYWORDS = frozenset({
    "if", "then", "elif", "else", "fi", "for", "while", "until", "do", "done",
    "case", "esac", "select", "function", "in", "{", "}", "((", "))", "[[", "]]",
})


class Word:
    """A shell word, kept as literal text plus the positions that could not be resolved.

    Keeping "unknown" as structure rather than collapsing it to a string is what lets the model
    say "the interpreter is a python, the directory is unknown" instead of having to choose
    between a confident wrong answer and no answer.
    """

    __slots__ = ("raw", "text", "resolved", "quoted")

    def __init__(self, raw: str, text: str, resolved: bool, quoted: bool):
        self.raw = raw
        self.text = text
        self.resolved = resolved
        self.quoted = quoted

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Word({self.text!r}, resolved={self.resolved})"


def strip_comment(line: str) -> tuple[str, str | None]:
    """Split a line into (code, comment). A `#` inside quotes is not a comment introducer."""
    out, comment = [], None
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch == "\\" and i + 1 < len(line):
            out.append(ch)
            out.append(line[i + 1])
            i += 2
            continue
        elif ch == "#" and (not out or out[-1].isspace()):
            comment = line[i:]
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out), comment


def _substitute(raw: str, env: dict[str, str | None]) -> tuple[str, bool]:
    """Expand ``$VAR`` / ``${VAR}`` from a bounded environment.

    Returns the expansion and whether it is complete. An unknown variable leaves a sentinel so a
    literal tail such as ``/scripts/smoke_http.py`` survives and remains matchable — losing the
    tail would turn a resolvable invocation into an unresolvable one for no reason.
    """
    complete = True

    if "$(" in raw or "`" in raw:
        # A command substitution is a nested command, not a value this layer can produce.
        complete = False

    def repl(m: re.Match) -> str:
        nonlocal complete
        name = m.group(1) or m.group(2)
        if name in env and env[name] is not None:
            return env[name]
        complete = False
        return "\x00UNKNOWN\x00"

    # Drop command substitutions to the sentinel first so their inner text cannot be mistaken for
    # a literal path fragment.
    text = re.sub(r"\$\((?:[^()]|\([^()]*\))*\)", "\x00UNKNOWN\x00", raw)
    text = re.sub(r"`[^`]*`", "\x00UNKNOWN\x00", text)
    text = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:[:#%/][^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)",
                  repl, text)
    return text, complete


def split_raw(code: str) -> list[str]:
    """Split into shell words, keeping quotes and command substitutions intact.

    ``shlex`` is not usable here: it has no notion of ``$( ... )`` and splits on the spaces inside
    one, which turned ``source "$(cd "$(dirname ...)" && pwd)/lib.sh"`` into the word ``$(cd``.
    Nesting is tracked explicitly so an inner substitution cannot end the outer one early.
    """
    words: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    depth = 0                                    # $( ) nesting
    i = 0
    while i < len(code):
        ch = code[i]
        if quote == "'":
            buf.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(code):
            buf.append(ch)
            buf.append(code[i + 1])
            i += 2
            continue
        if quote == '"':
            buf.append(ch)
            if ch == '"' and depth == 0:
                quote = None
            elif code.startswith("$(", i):
                depth += 1
                buf.append("(")
                i += 2
                continue
            elif ch == ")" and depth:
                depth -= 1
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if code.startswith("$(", i):
            depth += 1
            buf.append("$(")
            i += 2
            continue
        if ch == ")" and depth:
            depth -= 1
            buf.append(ch)
            i += 1
            continue
        if ch.isspace() and depth == 0:
            if buf:
                words.append("".join(buf))
                buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        words.append("".join(buf))
    return words


def command_substitutions(code: str) -> list[str]:
    """The bodies of every ``$( ... )`` at any nesting depth, outermost first."""
    out: list[str] = []
    i = 0
    while i < len(code):
        if code.startswith("$(", i):
            depth, j = 1, i + 2
            while j < len(code) and depth:
                if code.startswith("$(", j):
                    depth += 1
                    j += 2
                    continue
                if code[j] == ")":
                    depth -= 1
                    if not depth:
                        break
                elif code[j] == "(":
                    depth += 1
                j += 1
            body = code[i + 2:j]
            out.append(body)
            out.extend(command_substitutions(body))
            i = j + 1
            continue
        i += 1
    return out


def split_statements(code: str) -> list[str]:
    """Split on control operators at depth 0 only.

    A plain regex split was wrong in a way that mattered: the ``&&`` inside
    ``$(cd "$(dirname ...)" && pwd)`` is not a statement separator, and cutting there destroyed
    the `source` target before it could be resolved.
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    depth = 0
    i = 0
    while i < len(code):
        ch = code[i]
        if quote == "'":
            buf.append(ch)
            quote = None if ch == "'" else quote
            i += 1
            continue
        if ch == "\\" and i + 1 < len(code):
            buf.append(ch)
            buf.append(code[i + 1])
            i += 2
            continue
        if quote == '"':
            if code.startswith("$(", i):
                depth += 1
                buf.append("$(")
                i += 2
                continue
            if ch == ")" and depth:
                depth -= 1
            elif ch == '"' and depth == 0:
                quote = None
            buf.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if code.startswith("$(", i):
            depth += 1
            buf.append("$(")
            i += 2
            continue
        if ch == ")" and depth:
            depth -= 1
            buf.append(ch)
            i += 1
            continue
        if depth == 0:
            two = code[i:i + 2]
            if two in ("&&", "||", ";;"):
                parts.append("".join(buf))
                buf = []
                i += 2
                continue
            if ch in ";|&":
                parts.append("".join(buf))
                buf = []
                i += 1
                continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p for p in (x.strip() for x in parts) if p]


def split_words(code: str, env: dict[str, str | None]) -> list[Word]:
    """Split a command into words, expanding what the bounded environment knows."""
    raw_words = split_raw(code)
    out = []
    for rw in raw_words:
        quoted = len(rw) >= 2 and rw[0] == rw[-1] and rw[0] in "'\""
        inner = rw[1:-1] if quoted else rw
        if quoted and rw[0] == "'":
            out.append(Word(rw, inner, True, True))          # single quotes: no expansion at all
            continue
        text, complete = _substitute(inner, env)
        out.append(Word(rw, text, complete, quoted))
    return out


def _is_python(word: Word) -> bool:
    tail = word.text.split("\x00")[-1]
    return Path(tail).name in PYTHON_BASENAMES or tail.endswith(tuple(
        f"/{n}" for n in PYTHON_BASENAMES))


def _script_path(word: Word, *, base: Path | None = None) -> str | None:
    """The ``scripts/<name>.py`` a word denotes, if the file actually exists.

    Existence is checked against the repository AND against the directory the analysed script
    lives in, so a synthetic script in a test sandbox resolves the same way a real one does. A
    name that resolves to no file on either side is not a command root — that check is what stops
    an arbitrary string that merely looks like a path from becoming one.
    """
    m = re.search(r"scripts/([A-Za-z0-9_.-]+\.py)$", word.text)
    if not m:
        return None
    name = m.group(1)
    if (SCRIPTS / name).is_file():
        return name
    if base is not None and (base / name).is_file():
        return name
    return None


class ShellScript:
    """A bounded syntactic model of one shell script."""

    def __init__(self, path: Path):
        self.path = path
        try:
            # Scripts under test live outside the repository; a path that is simply not relative
            # to it is not an error, it just has no repository-relative name.
            self.rel = str(path.relative_to(REPO_ROOT))
        except ValueError:
            self.rel = str(path)
        self.text = path.read_text(encoding="utf-8", errors="replace")
        self.env: dict[str, str | None] = {}
        self.functions: dict[str, list[tuple[int, str]]] = {}
        self.commands: list[dict] = []
        self.mentions: list[dict] = []
        self.sourced: list[dict] = []
        self._parse()

    # -- syntax ---------------------------------------------------------------------------- #
    def _logical_lines(self):
        """(lineno, code, comment, context) with heredoc bodies and continuations handled."""
        raw = self.text.splitlines()
        i = 0
        pending: list[tuple[str, bool]] = []          # (tag, is_quoted) for heredocs
        heredoc_owner = None
        while i < len(raw):
            line = raw[i]
            if pending:
                tag, _q = pending[0]
                if line.strip() == tag:
                    pending.pop(0)
                    if not pending:
                        heredoc_owner = None
                else:
                    yield i + 1, "", None, ("heredoc", heredoc_owner)
                i += 1
                continue

            code, comment = strip_comment(line)
            # line continuations
            joined_from = i
            while code.rstrip().endswith("\\") and i + 1 < len(raw):
                i += 1
                more, more_comment = strip_comment(raw[i])
                code = code.rstrip()[:-1] + " " + more
                comment = comment or more_comment

            for m in re.finditer(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", code):
                pending.append((m.group(2), bool(m.group(1))))
                heredoc_owner = joined_from + 1

            yield joined_from + 1, code, comment, ("code", None)
            i += 1

    def _parse(self) -> None:
        fn_stack: list[tuple[str, int]] = []
        depth = 0
        # A branch whose condition is the literal `false` cannot run. Only literal `true`/`false`
        # are evaluated: anything richer would be an interpreter, and guessing a condition wrong
        # in the inclusive direction invents a root while guessing it wrong in the exclusive
        # direction hides one. Unknown conditions are therefore treated as LIVE, which fails
        # toward keeping a real control.
        dead = 0
        for lineno, code, comment, (kind, owner) in self._logical_lines():
            if comment:
                for m in re.finditer(r"scripts/([A-Za-z0-9_.-]+\.py)", comment):
                    self.mentions.append({
                        "file": self.rel, "line": lineno, "text": comment.strip(),
                        "syntax_context": "comment",
                        "classification": NONEXECUTABLE_MENTION,
                        "evidence": "the occurrence is inside a `#` comment, which the shell never "
                                    "evaluates",
                        "module": m.group(1)})
            if kind == "heredoc":
                src = self.text.splitlines()[lineno - 1]
                for m in re.finditer(r"scripts/([A-Za-z0-9_.-]+\.py)", src):
                    self.mentions.append({
                        "file": self.rel, "line": lineno, "text": src.strip(),
                        "syntax_context": "heredoc_body",
                        "classification": NONEXECUTABLE_MENTION,
                        "evidence": "the occurrence is inside a here-document body, which is data "
                                    "for the owning command, not a command position",
                        "owner_line": owner, "module": m.group(1)})
                continue
            if not code.strip():
                continue

            stripped_code = code.strip()
            if re.match(r"^(?:if|while|until)\s+false\s*;?\s*(?:then|do)?\s*$", stripped_code):
                dead += 1
                continue
            if dead:
                if re.match(r"^(?:fi|done)\s*;?\s*$", stripped_code):
                    dead -= 1
                elif re.match(r"^(?:else|elif\b.*)$", stripped_code) and dead == 1:
                    dead = 0                     # the else of a `false` branch DOES run
                else:
                    for m in re.finditer(r"scripts/([A-Za-z0-9_.-]+\.py)", stripped_code):
                        self.mentions.append({
                            "file": self.rel, "line": lineno, "text": stripped_code,
                            "syntax_context": "statically_dead_branch",
                            "classification": NONEXECUTABLE_MENTION,
                            "evidence": "inside a branch whose condition is the literal `false`, "
                                        "so the shell never reaches this command",
                            "module": m.group(1)})
                continue

            fn = re.match(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_-]*)\s*\(\s*\)\s*\{?\s*$",
                          code)
            if fn:
                fn_stack.append((fn.group(1), depth))
                self.functions.setdefault(fn.group(1), [])
                depth += code.count("{") - code.count("}")
                continue
            if fn_stack:
                self.functions[fn_stack[-1][0]].append((lineno, code))
                depth += code.count("{") - code.count("}")
                if depth <= fn_stack[-1][1]:
                    fn_stack.pop()
                continue

            self._statement(lineno, code, inside_function=None)

    # -- statements ------------------------------------------------------------------------ #
    def _statement(self, lineno: int, code: str, inside_function: str | None) -> None:
        # A command substitution IS a command position. Dropping its contents would be a false
        # EXCLUSION — `$(python3 scripts/x.py)` runs x.py just as surely as a bare line does —
        # and this model exists because the previous one got inclusion wrong in the other
        # direction. Both directions are defects.
        for inner in command_substitutions(code):
            for part in split_statements(inner):
                self._command(lineno, part.strip(), inside_function)
        for part in split_statements(code):
            part = part.strip()
            if not part:
                continue
            part = re.sub(r"^[({]\s*", "", part)
            part = re.sub(r"\s*[)}]$", "", part)
            self._command(lineno, part, inside_function)

    def _command(self, lineno: int, code: str, inside_function: str | None) -> None:
        words = split_words(code, self.env)
        if not words:
            return

        # leading assignments: `VAR=value cmd ...`
        idx = 0
        env_prefix: dict[str, str] = {}
        while idx < len(words) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[idx].raw):
            name, _, value = words[idx].raw.partition("=")
            val_word = split_words(value, self.env)
            resolved = val_word[0].text if val_word else ""
            env_prefix[name] = resolved
            idx += 1

        if idx >= len(words):
            # A pure assignment. It creates a VALUE, never an execution.
            for name, value in env_prefix.items():
                self.env[name] = value if "\x00" not in value else value
            for name, value in env_prefix.items():
                for m in re.finditer(r"scripts/([A-Za-z0-9_.-]+\.py)", value):
                    self.mentions.append({
                        "file": self.rel, "line": lineno, "text": code.strip(),
                        "syntax_context": "assignment",
                        "classification": NONEXECUTABLE_MENTION,
                        "evidence": f"assigned to {name}; an assignment binds a value and does not "
                                    "execute it. If the variable is later used in command "
                                    "position, that use is classified there.",
                        "module": m.group(1)})
            return

        head = words[idx]
        rest = words[idx + 1:]

        if head.text in _KEYWORDS:
            trailing = code.split(None, 1)
            if len(trailing) == 2:
                self._command(lineno, trailing[1], inside_function)
            return

        # `exec cmd`, `command cmd`, `time cmd` -> the next word is the real command.
        while head.text in COMMAND_PREFIXES and rest:
            if head.text == "eval":
                self._unresolved(lineno, code, "eval builds a command at runtime; this model "
                                               "refuses to guess what it would build")
                return
            head, rest = rest[0], rest[1:]

        if head.text in ("source", "."):
            self._source(lineno, rest, code)
            return

        if head.text in self.functions:
            for fl, fcode in self.functions[head.text]:
                self._statement(fl, fcode, inside_function=head.text)
            return

        # -- a python interpreter running a repository script: the case that matters
        if _is_python(head):
            script, argv, unresolved = None, [], False
            for w in rest:
                sp = _script_path(w, base=self.path.parent)
                if sp and script is None:
                    script = sp
                    continue
                if script is not None:
                    if w.resolved:
                        argv.append(w.text)
                    else:
                        unresolved = True
            if script:
                self.commands.append({
                    "file": self.rel, "line": lineno, "text": code.strip(),
                    "interpreter": head.text.replace("\x00UNKNOWN\x00", "<unresolved>"),
                    "interpreter_resolved": head.resolved,
                    "module": script, "argv": argv,
                    "argv_fully_resolved": not unresolved,
                    "env_prefix": env_prefix,
                    "inside_function": inside_function,
                    "classification": EXECUTABLE_INVOCATION,
                    "evidence": "command position; the executable resolves to a Python interpreter "
                                "and the argument denotes a repository script",
                })
                self.mentions.append({
                    "file": self.rel, "line": lineno, "text": code.strip(),
                    "syntax_context": "command_argument",
                    "classification": EXECUTABLE_INVOCATION,
                    "evidence": "argument of a python interpreter in command position",
                    "module": script})
                return

        # -- a nested shell script
        if head.text in SHELL_LAUNCHERS or head.text.endswith(".sh"):
            target = head.text if head.text.endswith(".sh") else (
                rest[0].text if rest else "")
            m = re.search(r"scripts/([A-Za-z0-9_.-]+\.sh)$", target)
            if m:
                # Resolve beside the analysed script first, then in the repository, so a nested
                # script in a test sandbox is followed exactly as a real one is.
                beside = self.path.parent / m.group(1)
                resolved = beside if beside.is_file() else (SCRIPTS / m.group(1))
                self.commands.append({
                    "file": self.rel, "line": lineno, "text": code.strip(),
                    "nested_shell_script": f"scripts/{m.group(1)}",
                    "nested_shell_path": str(resolved),
                    "classification": EXECUTABLE_INVOCATION,
                    "evidence": "command position; a shell launcher running another repository "
                                "shell script"})
                return

        # -- a data consumer: arguments are read, printed or copied, never executed
        if Path(head.text).name in DATA_CONSUMERS:
            for w in rest:
                for m in re.finditer(r"scripts/([A-Za-z0-9_.-]+\.py)", w.text):
                    self.mentions.append({
                        "file": self.rel, "line": lineno, "text": code.strip(),
                        "syntax_context": "data_consumer_argument",
                        "classification": NONEXECUTABLE_MENTION,
                        "evidence": f"argument of `{Path(head.text).name}`, which consumes its "
                                    "arguments as data",
                        "module": m.group(1)})
            return

        # -- anything else that names a python script is NOT proven either way
        for w in rest + [head]:
            for m in re.finditer(r"scripts/([A-Za-z0-9_.-]+\.py)", w.text):
                self.mentions.append({
                    "file": self.rel, "line": lineno, "text": code.strip(),
                    "syntax_context": "command_argument",
                    "classification": UNRESOLVED_MENTION,
                    "evidence": f"appears in a command whose executable {head.text!r} could not be "
                                "resolved to an interpreter, a shell launcher, or a known data "
                                "consumer",
                    "module": m.group(1)})

    def _unresolved(self, lineno: int, code: str, why: str) -> None:
        for m in re.finditer(r"scripts/([A-Za-z0-9_.-]+\.py)", code):
            self.mentions.append({
                "file": self.rel, "line": lineno, "text": code.strip(),
                "syntax_context": "dynamic",
                "classification": UNRESOLVED_MENTION,
                "evidence": why, "module": m.group(1)})
        self.commands.append({"file": self.rel, "line": lineno, "text": code.strip(),
                              "classification": UNRESOLVED_MENTION, "evidence": why})

    def _source(self, lineno: int, rest: list[Word], code: str) -> None:
        """`source path` / `. path`, resolved only when the target is unambiguous.

        The sourcing line in this repository is
        ``source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"`` — the directory is a
        command substitution this layer will not evaluate. The bounded rule is deliberately
        narrow: take the literal basename after the last `/`, and resolve it ONLY if a file of
        that name sits beside the sourcing script. Anything else is unresolved, because a
        wrong guess here silently imports the wrong variable bindings.
        """
        if not rest:
            return
        target = rest[0].text
        base = target.rsplit("/", 1)[-1].strip("\"'")
        candidate = self.path.parent / base
        if re.fullmatch(r"[A-Za-z0-9_.-]+\.sh", base) and candidate.is_file():
            child = ShellScript(candidate)
            for name, value in child.env.items():
                self.env.setdefault(name, value)
            self.functions.update({k: v for k, v in child.functions.items()
                                   if k not in self.functions})
            self.sourced.append({"line": lineno, "target": str(candidate.relative_to(REPO_ROOT)),
                                 "resolved": True,
                                 "rule": "literal basename beside the sourcing script"})
            return
        self.sourced.append({"line": lineno, "target": target, "resolved": False,
                             "rule": "no literal basename could be resolved beside the sourcing "
                                     "script"})
        self._unresolved(lineno, code, "a sourced file could not be resolved, so any bindings it "
                                       "provides are unknown")


_cache: dict[str, ShellScript] = {}


def analyse(path: Path | str) -> ShellScript:
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    key = str(p)
    if key not in _cache:
        _cache[key] = ShellScript(p)
    return _cache[key]


def reset_caches() -> None:
    _cache.clear()


def python_invocations(path: Path | str, *, _seen: frozenset[str] = frozenset()) -> list[dict]:
    """Every repository Python script this shell script runs, following nested shell scripts."""
    script = analyse(path)
    if script.rel in _seen:
        return []
    seen = _seen | {script.rel}
    out = []
    for cmd in script.commands:
        if cmd.get("module"):
            out.append({**cmd, "via": list(seen)})
        elif cmd.get("nested_shell_script"):
            nested = Path(cmd.get("nested_shell_path") or (REPO_ROOT / cmd["nested_shell_script"]))
            if nested.is_file():
                for inner in python_invocations(nested, _seen=seen):
                    out.append({**inner, "via": list(seen) + inner.get("via", [])})
    return out


def unresolved_mentions(path: Path | str) -> list[dict]:
    return [m for m in analyse(path).mentions
            if m["classification"] == UNRESOLVED_MENTION]


def main() -> int:  # pragma: no cover - operator convenience
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("script")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    s = analyse(args.script)
    payload = {"script": s.rel, "commands": s.commands, "mentions": s.mentions,
               "sourced": s.sourced, "assignments": s.env,
               "python_invocations": python_invocations(args.script)}
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=True))
    else:
        for c in s.commands:
            print(f"  {c['file']}:{c['line']}  {c['classification']}  "
                  f"{c.get('module') or c.get('nested_shell_script') or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
