#!/usr/bin/env python3
"""Bounded semantic model of COMMAND POSITIONS in shell source (Gate 4N-I28AO).

THE DEFECT THIS CLOSES. Gate 4N-I28AN finding ADV-I28AN-01. The previous shell derivation took
`stripped.split()[0]` — the first token of each line — and discarded the line whenever that token
contained `=`, began with `-`, or was a builtin. It never split on `|`, `&&`, `||` or `;`, and never
descended into `$( )`. So `uid="$(docker run ...)"` yielded nothing, `false | tee log` yielded
nothing, and `if [ 1 ] && grep -q x f` yielded nothing. `docker`, `seq`, `grep`, `tee`, `mktemp` and
`dirname` were all invoked by tracked shell and absent from the trust policy, while both the
inventory check and the trust check reported clean.

WHAT THIS IS, STATED HONESTLY. This is NOT a complete Bash parser and does not claim to be. It is a
bounded tokeniser plus a command-position grammar over the constructs this repository's shell
actually uses. Every construct it does not model is reported as UNSUPPORTED and fails closed: an
unsupported construct that could carry an executable is an error, never a silent skip. The
supported and unsupported grammars are both enumerable — see `supported_forms()` and
`unsupported_forms()` — so a reader can check the claim rather than take it.

THE MODEL. A word is in COMMAND POSITION when it is the first word of a simple command. A simple
command begins:
  * at the start of input, after a newline, or after `;`, `&`, `|`, `||`, `&&`
  * after an opening `(`, `{`, `$(`, `` ` ``, `<(`, `>(`
  * after a keyword that introduces a command: if then elif else while until do !  time
  * after `;;` inside a case body, and after the `)` that closes a case pattern
  * after leading assignment words (`VAR=value`), which are prefixes, not commands
  * after a COMMAND WRAPPER (`env`, `command`, `sudo`, `nice`, `nohup`, `xargs`, `time`,
    `timeout`, `stdbuf`), whose own operands resume command position
Everything else is an argument, a redirection target, a pattern, or an assignment word.

WHAT IS DELIBERATELY INERT. Comment bodies, heredoc BODIES (but not the heredoc opener line, which
still carries a command), single- and double-quoted strings that are not a nested-shell operand,
and `for x in ...` word lists. `echo docker run` yields `echo`, not `docker`.

FAIL-CLOSED CASES. `eval`, a command word that is a variable expansion, an unterminated quote, an
unterminated heredoc, and any construct in `unsupported_forms()` are recorded as UNRESOLVED with a
reason. The caller decides what to do; `executable_inventory` refuses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- vocabulary
KEYWORDS_INTRODUCING_A_COMMAND = frozenset({
    "if", "then", "elif", "else", "while", "until", "do", "!", "time",
})
KEYWORDS_TERMINATING = frozenset({"fi", "done", "esac", "then", "do", "else", "elif", "in"})
ALL_KEYWORDS = KEYWORDS_INTRODUCING_A_COMMAND | KEYWORDS_TERMINATING | frozenset({
    "for", "case", "select", "function", "coproc", "esac", "fi", "done",
})

# A wrapper consumes its own options and then the NEXT word is again a command.
COMMAND_WRAPPERS = frozenset({
    "env", "command", "sudo", "nice", "nohup", "xargs", "time", "timeout", "stdbuf", "builtin",
})

# Interpreters whose `-c` operand is nested shell source.
NESTED_SHELL = frozenset({"bash", "sh", "zsh", "dash", "ksh"})

# Constructs that carry an executable this model cannot resolve. Recorded, never skipped.
FAIL_CLOSED_WORDS = frozenset({"eval", "source", "."})

SHELL_BUILTINS = frozenset({
    "echo", "cd", "export", "local", "read", "set", "unset", "shift", "return", "exit", "trap",
    "exec", "test", "true", "false", "printf", "pwd", "wait", "kill", "declare", "typeset",
    "readonly", "let", "shopt", "alias", "unalias", "type", "hash", "umask", "ulimit", "jobs",
    "bg", "fg", "getopts", "continue", "break", ":", "mapfile", "readarray", "pushd", "popd",
    "dirs", "history", "help", "logout", "times", "caller", "enable", "disable", "compgen",
    "complete", "compopt", "bind", "fc", "suspend", "[", "[[", "]]", "]", "command",
})

# GATE 4N-I28BB, closing the load-bearing half of ADV-I28AX-01. `exec` REPLACES the shell with its
# operand, so the operand is a command position. It was in SHELL_BUILTINS and not in
# COMMAND_WRAPPERS, so it took the builtin branch, recorded `exec` (which executables() then filters
# out as a builtin) and set expecting=False — silently discarding the child. `exec kubectl` derived
# NOTHING while reporting COMPLETE, trustworthy, 0 unresolved, 0 unsupported, 0 parse errors.
#
# The option table is EXPLICIT and arity-bearing. A generic "skip tokens beginning with -" rule is
# what makes `exec -a kubectl docker run` read `kubectl` as the child when it is the value of -a,
# and it silently accepts any future option bash may add. Anything not in this table fails closed.
EXEC_OPTIONS = {"-a": 1, "-c": 0, "-l": 0}
# Only these single letters may combine (`exec -cl cmd`). `-a` takes a value and never combines.
EXEC_COMBINABLE = frozenset("cl")

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
_PLAIN_WORD = re.compile(r"^[A-Za-z0-9_./+-]+$")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_REDIRECTION = re.compile(r"^[0-9]*(>>?|<<<|<)&?[0-9-]*$")
_DUP_REDIRECT = re.compile(r"[0-9]*[<>]&[0-9]+-?|[0-9]*[<>]&-")
_LOOKS_LIKE_SHELL = re.compile(r"\|\||&&|[|;]|\$\(")


@dataclass
class Command:
    word: str
    line: int
    construct: str
    resolved: bool = True
    reason: str = ""


@dataclass
class TransferSite:
    """A word that hands command position to a following word.

    GATE 4N-I28BB. Recorded for EVERY exec, static or not, so that "this source contains a
    command-position transfer" is a positive, countable fact. An omitted child is then a MISSING
    RECORD rather than an absence indistinguishable from inert text — which is exactly how
    ADV-I28AX-01 stayed invisible: `# exec kubectl` and `exec kubectl` both derived nothing.
    """
    word: str                       # the transferring word, e.g. "exec"
    line: int
    child: str                      # the child token as written ("" when there is none)
    classification: str             # STATIC_CHILD_DISCOVERED | DYNAMIC_CHILD_UNRESOLVED |
    #                                 EXEC_WITHOUT_CHILD | UNSUPPORTED_AND_FAIL_CLOSED |
    #                                 MALFORMED_AND_FAIL_CLOSED
    reason: str = ""
    options: tuple = ()


@dataclass
class ScanResult:
    commands: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    unsupported: list = field(default_factory=list)
    transfer_sites: list = field(default_factory=list)

    # GATE 4N-I28AV completeness contract. ADV-I28AT-01 was not merely a state-reset bug: NOTHING
    # in the result could have revealed the omission. The loop ran to the final token and simply
    # skipped every word, so by its own reckoning it succeeded. These fields exist so that a skip
    # of ANY kind — this one or a future one — cannot again be reported as success.
    status: str = "COMPLETE"
    source_length: int = 0
    start_position: int = 0
    consumed_position: int = 0
    eof_reached: bool = True
    unconsumed_ranges: list = field(default_factory=list)
    open_frames: list = field(default_factory=list)
    parse_errors: list = field(default_factory=list)
    words_seen: int = 0
    words_skipped: int = 0

    def is_trustworthy(self) -> bool:
        """The ONLY two statuses a trust decision may consume.

        `COMPLETE_WITH_DECLARED_UNRESOLVED` is permitted because every unresolved construct is
        separately adjudicated and fails closed where it matters. PARTIAL, UNSUPPORTED, MALFORMED
        and INTERNAL_ERROR are never permitted, and absence of an exception is not completeness.
        """
        return (self.status in ("COMPLETE", "COMPLETE_WITH_DECLARED_UNRESOLVED")
                and self.eof_reached
                and not self.unconsumed_ranges
                and not self.open_frames
                and not self.parse_errors)

    def completeness_problems(self) -> list:
        """Why this result may not be trusted, in terms a caller can act on."""
        problems = []
        if self.status not in ("COMPLETE", "COMPLETE_WITH_DECLARED_UNRESOLVED"):
            problems.append(f"parse status is {self.status}, which is not a permitted trust input")
        if not self.eof_reached:
            problems.append(
                f"the scan stopped at byte {self.consumed_position} of {self.source_length} "
                "without reaching EOF, so the remaining source was never examined")
        for start, end in self.unconsumed_ranges:
            problems.append(f"source bytes {start}..{end} were not consumed")
        for frame in self.open_frames:
            problems.append(f"the `{frame}` construct was still open at end of input")
        for err in self.parse_errors:
            problems.append(f"parse error: {err}")
        return problems


    def executables(self, functions=frozenset()) -> set:
        return {c.word for c in self.commands
                if c.resolved and c.word not in SHELL_BUILTINS and c.word not in functions
                and c.word not in ALL_KEYWORDS}


def supported_forms() -> list:
    """The grammar this model claims. Enumerated so the claim can be checked."""
    return [
        "simple command", "leading assignment words", "pipeline |", "AND list &&", "OR list ||",
        "list ;", "background &", "subshell ( ... )", "group { ...; }",
        "command substitution $( ... )", "backtick substitution ` ... `",
        "process substitution <( ... ) and >( ... )",
        "if/then/elif/else/fi", "while/until/do/done", "for/do/done", "case/in/)/;;/esac",
        "function definition and body", "trap 'body' SIG", "heredoc opener command",
        "line continuation", "quoted command word", "nested shell via bash -c / sh -c",
        "command wrappers (env, command, sudo, nice, nohup, xargs, time, timeout, stdbuf)",
        "shebang interpreter", "/usr/bin/env interpreter",
        # GATE 4N-I28BB
        "exec with a static child command (exec CMD, exec -- CMD, exec -a NAME CMD, exec -c CMD, "
        "exec -l CMD, and combinations of -c/-l)",
        "exec with redirections only and no child (exec 3< file, exec > log)",
        "exec with a dynamic child command, recorded as an explicit unresolved transfer site",
    ]


# GATE 4N-I28BB. Bound into the session baseline and re-derived at session finish, so a later edit
# to the grammar or the option table is a detectable change rather than a silent widening.
EXEC_GRAMMAR_VERSION = "i28bb.1"


def exec_grammar_contract() -> dict:
    """The exec model, as data, so a control can assert it instead of trusting the code."""
    return {
        "version": EXEC_GRAMMAR_VERSION,
        "transferring_words": ["exec"],
        "options": dict(EXEC_OPTIONS),
        "combinable_flags": sorted(EXEC_COMBINABLE),
        "terminator": "--",
        "classifications": [
            "STATIC_CHILD_DISCOVERED", "DYNAMIC_CHILD_UNRESOLVED", "EXEC_WITHOUT_CHILD",
            "UNSUPPORTED_AND_FAIL_CLOSED", "MALFORMED_AND_FAIL_CLOSED",
        ],
        "unsupported_transferring_words": ["coproc"],
        "rules": [
            "an option not in the table fails closed; there is no skip-tokens-beginning-with-hyphen rule",
            "-a consumes exactly one value and that value is never the child",
            "-- terminates option parsing; the next word is the child even if it looks like an option",
            "a dynamic child is an explicit unresolved transfer site, never a static identity",
            "every exec records a transfer site, so an omitted child is a missing record",
            "coproc is UNSUPPORTED_AND_FAIL_CLOSED and cannot reach a permitted trust status",
        ],
    }


def unsupported_forms() -> list:
    """Constructs this model does NOT resolve. Each fails closed rather than being skipped."""
    return [
        "eval with a computed string",
        "source / . of a computed path",
        "a command word that is a variable expansion ($cmd, ${cmd}, $(...) as the word itself)",
        "arrays expanded as the command word (\"${CMD[@]}\")",
        "aliases (bash does not expand them in non-interactive scripts, so none is modelled)",
        "arithmetic command (( ... ))",
        "coproc",
        "unterminated quote or heredoc",
    ]


# --------------------------------------------------------------------------- tokenising
class _Tokeniser:
    """Splits shell source into words and operators, honouring quoting and heredocs."""

    OPERATORS = ("&&", "||", ";;", ";", "|", "&", "(", ")", "{", "}", "\n")

    def __init__(self, src: str):
        self.src = src
        self.i = 0
        self.line = 1
        self.pending_heredocs: list = []
        self.problems: list = []

    def _skip_heredoc_bodies(self):
        while self.pending_heredocs:
            term = self.pending_heredocs.pop(0)
            end = re.compile(rf"^\s*{re.escape(term)}\s*$", re.M)
            m = end.search(self.src, self.i)
            if not m:
                self.problems.append((self.line, f"unterminated heredoc <<{term}"))
                self.i = len(self.src)
                return
            self.line += self.src.count("\n", self.i, m.end())
            self.i = m.end()

    def tokens(self):
        out = []
        while self.i < len(self.src):
            ch = self.src[self.i]
            if ch == "\n":
                out.append(("op", "\n", self.line, []))
                self.i += 1
                self.line += 1
                self._skip_heredoc_bodies()
                continue
            if ch in " \t":
                self.i += 1
                continue
            if ch == "\\" and self.i + 1 < len(self.src) and self.src[self.i + 1] == "\n":
                self.i += 2
                self.line += 1
                continue
            if ch == "#" and (not out or out[-1][0] == "op" or self.src[self.i - 1] in " \t\n"):
                nl = self.src.find("\n", self.i)
                self.i = len(self.src) if nl < 0 else nl
                continue
            # A duplicating redirection is ONE token. Splitting `2>&1` on `&` made `1` a command
            # word — which is how `false 2>&1 | tee log` reported an executable called `1`.
            dup = _DUP_REDIRECT.match(self.src, self.i)
            if dup:
                out.append(("word", dup.group(0), self.line, []))
                self.i = dup.end()
                continue
            two = self.src[self.i:self.i + 2]
            if two in ("&&", "||", ";;"):
                out.append(("op", two, self.line, [])); self.i += 2; continue
            if two in ("<(", ">("):
                out.append(("op", two, self.line, [])); self.i += 2; continue
            if ch in ";|&(){}":
                out.append(("op", ch, self.line, [])); self.i += 1; continue
            word, embedded = self._read_word()
            if word or embedded:
                out.append(("word", word, self.line, embedded))
        return out

    def _matching_paren(self, open_at: int) -> int:
        """Index just past the `)` that closes the `$(` or `(` beginning at open_at.

        Depth-aware and quote-aware, because `"$(dirname "$0")/helper.sh"` nests both.
        """
        depth = 0
        j = open_at
        while j < len(self.src):
            c = self.src[j]
            if c == "\\":
                j += 2; continue
            if c == "'":
                e = self.src.find("'", j + 1)
                j = len(self.src) if e < 0 else e + 1
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return -1

    def _read_word(self):
        """One word, plus any command substitutions embedded ANYWHERE inside it.

        GATE 4N-I28AO. A first version consumed `"..."` wholesale, so `uid="$(docker run x)"`,
        `D="$(mktemp -d)"` and `"$(dirname "$0")/helper.sh"` all yielded nothing — the very shape
        ADV-I28AN-01 is about. Substitutions are now extracted from inside quotes and from mid-word
        positions and handed back for a recursive scan.
        """
        start = self.i
        buf = []
        embedded: list = []
        sub_spans: list = []          # source ranges occupied by substitutions
        while self.i < len(self.src):
            ch = self.src[self.i]
            two = self.src[self.i:self.i + 2]
            if self.src[self.i:self.i + 3] == "$((":
                # arithmetic expansion, not a command substitution. `failures=$((failures + 1))`
                # reported an executable called `failures` until this was distinguished.
                end = self.src.find("))", self.i + 3)
                self.i = len(self.src) if end < 0 else end + 2
                buf.append("\x00ARITH\x00")
                continue
            if two == "$(":
                end = self._matching_paren(self.i + 1)
                if end < 0:
                    self.problems.append((self.line, "unterminated command substitution"))
                    self.i = len(self.src)
                    break
                embedded.append(self.src[self.i + 2:end - 1])
                sub_spans.append((self.i, end))
                buf.append("\x00SUB\x00")
                self.i = end
                continue
            if ch == "`":
                end = self.src.find("`", self.i + 1)
                if end < 0:
                    self.problems.append((self.line, "unterminated backtick substitution"))
                    self.i = len(self.src)
                    break
                embedded.append(self.src[self.i + 1:end])
                sub_spans.append((self.i, end + 1))
                buf.append("\x00SUB\x00")
                self.i = end + 1
                continue
            if ch in " \t\n;|&()":
                break
            if (ch == "{" or ch == "}") and not buf:
                break
            if ch == "\\":
                if self.i + 1 < len(self.src):
                    buf.append(self.src[self.i + 1]); self.i += 2; continue
                self.i += 1; continue
            if ch == "'":
                end = self.src.find("'", self.i + 1)
                if end < 0:
                    self.problems.append((self.line, "unterminated single quote"))
                    self.i = len(self.src)
                    break
                buf.append(self.src[self.i + 1:end]); self.i = end + 1; continue
            if ch == '"':
                j = self.i + 1
                inner = []
                while j < len(self.src) and self.src[j] != '"':
                    if self.src[j] == "\\":
                        inner.append(self.src[j + 1:j + 2]); j += 2; continue
                    if self.src[j:j + 2] == "$(":
                        e = self._matching_paren(j + 1)
                        if e < 0:
                            break
                        embedded.append(self.src[j + 2:e - 1])
                        sub_spans.append((j, e))
                        inner.append("\x00SUB\x00"); j = e; continue
                    if self.src[j] == "`":
                        e = self.src.find("`", j + 1)
                        if e < 0:
                            break
                        embedded.append(self.src[j + 1:e])
                        sub_spans.append((j, e + 1))
                        inner.append("\x00SUB\x00"); j = e + 1; continue
                    inner.append(self.src[j]); j += 1
                if j >= len(self.src) or self.src[j] != '"':
                    self.problems.append((self.line, "unterminated double quote"))
                    self.i = len(self.src)
                    break
                buf.append("".join(inner)); self.i = j + 1; continue
            buf.append(ch); self.i += 1
        word = "".join(buf)
        # A heredoc opener INSIDE a substitution belongs to the nested shell, whose own scan
        # consumes its body. Registering it on the outer tokeniser made
        # `head="$(python - <<\'PY\' ... PY)"` report an unterminated heredoc, because the
        # terminator had already been consumed by the inner scan.
        raw_chars = []
        pos = start
        for a, b in sub_spans:
            raw_chars.append(self.src[pos:a])
            pos = b
        raw_chars.append(self.src[pos:self.i])
        raw_outside = "".join(raw_chars)
        m = _HEREDOC.search(raw_outside)
        if m:
            self.pending_heredocs.append(m.group(2))
        return word, embedded


# --------------------------------------------------------------------------- the grammar
def scan(src: str, *, origin: str = "<shell>") -> ScanResult:
    """Command positions in one shell source string."""
    result = ScanResult()
    tok = _Tokeniser(src)
    tokens = tok.tokens()
    for line, why in tok.problems:
        result.unresolved.append(Command("<unparsed>", line, "tokeniser", False, why))
        # GATE 4N-I28AV. A tokeniser problem — an unterminated quote or heredoc — is a genuine
        # PARSE FAILURE, not a declared-unresolved dynamic command word. Recording it only as
        # "unresolved" let a malformed source reach COMPLETE_WITH_DECLARED_UNRESOLVED, which is a
        # permitted trust input. It is now also a parse error, so the status becomes MALFORMED and
        # the result fails closed. It stays in `unresolved` as well, so existing consumers that
        # enumerate unresolved constructs are unchanged.
        result.parse_errors.append(f"line {line}: {why}")

    expecting = True          # next word is a command position
    construct = "simple command"
    in_for_words = False
    in_case_pattern = False
    case_awaiting_in = 0        # GATE 4N-I28AV: `case` seen, its `in` not yet seen
    # GATE 4N-I28AO: a `)` that closes a subshell or a process substitution RESTORES the enclosing
    # position; it does not open a new command. Without this stack, `diff <(docker run x) f` read
    # `f` — an argument of diff — as a command word.
    paren_stack: list = []
    idx = 0
    while idx < len(tokens):
        kind, val, line, embedded = tokens[idx]
        idx += 1
        if kind == "op":
            if val in ("\n", ";", "&", "|", "&&", "||", "(", "{", "<(", ">(", ";;"):
                if val in ("(", "<(", ">("):
                    paren_stack.append((expecting, construct))
                expecting = True
                in_for_words = False
                if val in ("<(", ">("):
                    construct = "process substitution"
                elif val == "|":
                    construct = "pipeline"
                elif val == "&&":
                    construct = "AND list"
                elif val == "||":
                    construct = "OR list"
                elif val == "(":
                    construct = "subshell"
                elif val == "{":
                    construct = "group"
                elif val == ";;":
                    construct = "case branch"
                    in_case_pattern = True
                else:
                    construct = "simple command"
            elif val == ")":
                if in_case_pattern:
                    in_case_pattern = False
                    expecting = True
                    construct = "case branch body"
                elif paren_stack:
                    expecting, construct = paren_stack.pop()
                else:
                    expecting = True
                    construct = "simple command"
            elif val == "}":
                expecting = True
                construct = "simple command"
            continue

        # kind == "word"
        # GATE 4N-I28AO: a substitution embedded ANYWHERE in a word — including inside double
        # quotes and mid-word — is nested shell and is scanned. `uid="$(docker run x)"`,
        # `D="$(mktemp -d)"` and `"$(dirname "$0")/helper.sh"` all live here, and all three were
        # invisible to the previous derivation.
        for inner in embedded:
            nested = scan(inner, origin=f"{origin} (substitution)")
            for c in nested.commands:
                result.commands.append(Command(c.word, line, "command substitution"))
            for c in nested.unresolved:
                result.unresolved.append(Command(c.word, line, "command substitution", False, c.reason))
        if in_for_words:
            continue
        if in_case_pattern:
            # GATE 4N-I28AV, closing ADV-I28AT-01. `esac` is itself a WORD, so this skip used to
            # swallow the very token that should end it — the terminator branch further down was
            # unreachable while the flag was armed. Disarm HERE, before skipping.
            #
            # The last `;;` of a case re-arms the flag for a pattern that never arrives; without
            # this, every remaining word in the scan is skipped and the partial result is returned
            # as COMPLETE. That is how `kubectl`, `helm` and `curl … | sh` sat in a graded,
            # release-blocking workflow step discovered by nothing.
            if val in ("esac", "fi", "done"):
                in_case_pattern = False
                expecting = True
                construct = "simple command"
            elif val == "in" and case_awaiting_in:
                # `in` arrives INSIDE the pattern skip — the same reason `esac` did — so the
                # obligation must be discharged here or every healthy `case` looks malformed.
                case_awaiting_in -= 1
            continue
        if not expecting:
            continue

        if _REDIRECTION.match(val):
            # `2>&1` is self-contained; `> file` and `2> file` consume the next word as a target.
            if not val.endswith(("&1", "&2")) and idx < len(tokens) and tokens[idx][0] == "word":
                idx += 1
            continue
        if val == "in":
            in_for_words = True
            if case_awaiting_in:
                case_awaiting_in -= 1
            continue
        if val in ("for", "select"):
            expecting = False
            continue
        if val == "case":
            in_case_pattern = True
            expecting = False
            # GATE 4N-I28AV. A `case` must be followed by `in` before its first pattern. Real bash
            # rejects `case "$x" a) … esac`, and the corpus oracle caught that this parser returned
            # COMPLETE for it — a malformed source reaching a permitted trust status. Recording the
            # obligation here lets the balance check below fail it closed.
            case_awaiting_in += 1
            continue
        if val == "function":
            expecting = False
            continue
        if val in KEYWORDS_TERMINATING or val in ("esac", "fi", "done"):
            # GATE 4N-I28AV, closing ADV-I28AT-01. `esac` MUST disarm the pattern skip.
            #
            # The last `;;` before `esac` re-arms `in_case_pattern` for a pattern that never
            # arrives, and this branch used to set `expecting` without clearing it. Line ~396
            # (`if in_case_pattern: continue`) then skipped EVERY remaining word in the scan — so
            # `kubectl`, `helm` and `curl … | sh` could sit in a graded, release-blocking workflow
            # step and be discovered by nothing, with 0 unresolved, 0 unsupported and no error.
            #
            # `fi` and `done` clear it too. Not because a `case` can legally end with them, but
            # because if the flag is somehow armed when a block terminator arrives, staying armed is
            # the failure mode that produced this finding.
            if val in ("esac", "fi", "done"):
                in_case_pattern = False
            expecting = True
            continue
        if val in KEYWORDS_INTRODUCING_A_COMMAND:
            expecting = True
            construct = f"after keyword `{val}`"
            continue
        if _ASSIGNMENT.match(val):
            construct = "after assignment word"
            continue                                  # still a command position
        if val.startswith(("$", "${")) or "$" in val[:2]:
            result.unresolved.append(Command(val, line, construct, False,
                                             "command word is a variable expansion"))
            # ADDITIVE, never permissive: the construct STILL fails closed above. But an operand
            # that is itself shell source is scanned anyway, because `"${CI_SHELL[@]}" 'false |
            # tee "$0"'` really does execute `tee`, and discovering it is strictly better than
            # relying on the fail-closed record alone.
            for j in range(idx, min(idx + 3, len(tokens))):
                if tokens[j][0] != "word":
                    break
                operand = tokens[j][1]
                # ONLY an operand that carries shell METACHARACTERS is treated as shell source.
                # Without this test the scan read `-m alembic upgrade` — plain arguments of
                # `$VENV_PY` — as commands and invented `alembic`, `upgrade` and `tests/`.
                if not _LOOKS_LIKE_SHELL.search(operand):
                    continue
                inner = scan(operand, origin=f"{origin} (operand of an unresolved wrapper)")
                for c in inner.commands:
                    if c.word not in SHELL_BUILTINS:
                        result.commands.append(
                            Command(c.word, line, "operand of an unresolved command word"))
            expecting = False
            continue
        if val in FAIL_CLOSED_WORDS:
            result.unresolved.append(Command(val, line, construct, False,
                                             f"`{val}` executes a computed operand"))
            expecting = False
            continue
        if val.endswith("()"):                        # `name()` function definition
            expecting = False
            continue
        if idx < len(tokens) and tokens[idx][0] == "op" and tokens[idx][1] == "(" \
                and idx + 1 < len(tokens) and tokens[idx + 1][1] == ")":
            expecting = False                          # `name ()` function definition
            idx += 2
            continue
        # `trap '<shell>' SIG` — the first operand is executed when the signal fires. It is a
        # builtin, so it is not itself an executable, but its body is real shell.
        if val == "trap" and idx < len(tokens) and tokens[idx][0] == "word":
            nested = scan(tokens[idx][1], origin=f"{origin} (trap body)")
            for c in nested.commands:
                result.commands.append(Command(c.word, line, "trap body"))
            result.unresolved.extend(nested.unresolved)
            idx += 1
            expecting = False
            continue
        # GATE 4N-I28BB. `coproc` runs its operand as a command in a background subshell, so it IS
        # a command-position transfer. This gate deliberately does NOT implement it: the compound
        # forms (`coproc NAME { ...; }`) need frame tracking this model does not have, and a
        # half-modelled transfer is worse than a declared refusal. It fails closed instead, which
        # makes the status UNSUPPORTED and therefore not a permitted trust input. `unsupported_forms`
        # already claimed this; before this gate the claim was false — `coproc kubectl` returned
        # COMPLETE and trustworthy with nothing recorded.
        if val == "coproc":
            result.unsupported.append(Command(
                val, line, construct, False,
                "`coproc` hands command position to its operand; this model does not implement "
                "coprocess parsing, so the construct fails closed (COPROC-I28BB-01)"))
            result.transfer_sites.append(TransferSite(
                val, line, tokens[idx][1] if idx < len(tokens) and tokens[idx][0] == "word" else "",
                "UNSUPPORTED_AND_FAIL_CLOSED", "coproc is not implemented by this model"))
            expecting = False
            continue

        # GATE 4N-I28BB, closing the load-bearing half of ADV-I28AX-01.
        if val == "exec":
            result.commands.append(Command(val, line, construct))
            j = idx
            opts: list = []
            failed = None
            saw_terminator = False
            while j < len(tokens) and tokens[j][0] == "word":
                w = tokens[j][1]
                if w == "--":
                    opts.append(w)
                    j += 1
                    saw_terminator = True
                    break                      # every later word is an operand, option-shaped or not
                if _REDIRECTION.match(w):
                    # `exec > log 2>&1` redirects the CURRENT shell and has no child at all.
                    j += 1
                    if not w.endswith(("&1", "&2")) and j < len(tokens) and tokens[j][0] == "word":
                        j += 1
                    continue
                if not w.startswith("-") or w == "-":
                    break                      # the child
                if w in EXEC_OPTIONS:
                    opts.append(w)
                    j += 1
                    for _ in range(EXEC_OPTIONS[w]):
                        if j >= len(tokens) or tokens[j][0] != "word":
                            failed = f"`exec {w}` requires a value and none follows"
                            break
                        opts.append(tokens[j][1])
                        j += 1
                    if failed:
                        break
                    continue
                if len(w) > 1 and all(ch in EXEC_COMBINABLE for ch in w[1:]):
                    opts.append(w)             # `-cl`, `-lc` — value-less flags may combine
                    j += 1
                    continue
                failed = (f"unknown or ambiguous `exec` option {w!r}; the supported set is "
                          f"{sorted(EXEC_OPTIONS)} plus combinations of {sorted(EXEC_COMBINABLE)}")
                break
            if failed:
                result.unsupported.append(Command(val, line, construct, False, failed))
                result.transfer_sites.append(TransferSite(
                    val, line, "", "UNSUPPORTED_AND_FAIL_CLOSED", failed, tuple(opts)))
                expecting = False
                continue
            child = tokens[j][1] if j < len(tokens) and tokens[j][0] == "word" else ""
            if not child:
                # Legal and common: `exec 3< file`, `exec > log`. It starts no process, so there is
                # no executable to discover — but it is still RECORDED, so "no child" is an asserted
                # classification rather than an absence.
                result.transfer_sites.append(TransferSite(
                    val, line, "", "EXEC_WITHOUT_CHILD",
                    "exec with redirections only; replaces no process", tuple(opts)))
                expecting = False
                idx = j
                continue
            child_embedded = tokens[j][3] if j < len(tokens) else []
            if child.startswith(("$", "${")) or "$" in child[:2] or "`" in child or child_embedded:
                # The three tracked sites in this repository are all `exec "$VENV_PY"`. A dynamic
                # target must NOT be invented as a static identity and must NOT vanish: it becomes an
                # explicit unresolved transfer site that policy completeness has to adjudicate.
                #
                # `child_embedded` catches `exec "$(resolve_cmd)"`, whose word the tokeniser replaces
                # with a placeholder carrying the inner source. Without it that child failed the
                # plain-word test and was labelled MALFORMED — still fail-closed, but the wrong
                # classification, and the substitution's own commands went unscanned.
                reason = "the `exec` child command is a variable expansion, so it cannot be resolved"
                result.unresolved.append(Command(child, line, "operand of `exec`", False, reason))
                result.transfer_sites.append(TransferSite(
                    val, line, child, "DYNAMIC_CHILD_UNRESOLVED", reason, tuple(opts)))
                for inner in child_embedded:
                    # `$(resolve_cmd)` really does execute `resolve_cmd`; the target is unresolved,
                    # the substitution's own commands are not.
                    nested = scan(inner, origin=f"{origin} (exec child substitution)")
                    for c in nested.commands:
                        result.commands.append(Command(c.word, line, "exec child substitution"))
                    result.unresolved.extend(nested.unresolved)
                    result.unsupported.extend(nested.unsupported)
                expecting = False
                idx = j + 1
                continue
            if not _PLAIN_WORD.match(child):
                reason = f"the `exec` child command {child!r} is not a plain word"
                result.unresolved.append(Command(child, line, "operand of `exec`", False, reason))
                result.transfer_sites.append(TransferSite(
                    val, line, child, "MALFORMED_AND_FAIL_CLOSED", reason, tuple(opts)))
                expecting = False
                idx = j + 1
                continue
            # Static child. Hand the position back to the MAIN LOOP rather than recording the child
            # here: that is what makes `exec docker run --privileged`, `exec npm ci`,
            # `exec bash -c '...'` and `exec env FOO=1 kubectl` flow through the existing Docker,
            # npm, nested-shell and wrapper machinery unchanged, instead of needing a second copy of
            # it that could drift.
            result.transfer_sites.append(TransferSite(
                val, line, child, "STATIC_CHILD_DISCOVERED", "", tuple(opts)))
            idx = j
            expecting = True
            construct = "operand of `exec`" + (" after --" if saw_terminator else "")
            continue

        if val in SHELL_BUILTINS and val not in COMMAND_WRAPPERS:
            # `command` and `builtin` are builtins AND wrappers: they hand command position to
            # their operand, so they must not short-circuit here.
            result.commands.append(Command(val, line, construct))
            expecting = False
            continue
        if not _PLAIN_WORD.match(val):
            result.unresolved.append(Command(val, line, construct, False,
                                             "command word is not a plain word"))
            expecting = False
            continue

        result.commands.append(Command(val, line, construct))
        # a wrapper hands command position to its first non-option, non-assignment operand
        if val in COMMAND_WRAPPERS:
            wrapper_pending = True
            construct = f"operand of wrapper `{val}`"
            # consume options and assignment words
            while idx < len(tokens) and tokens[idx][0] == "word" and (
                    tokens[idx][1].startswith("-") or _ASSIGNMENT.match(tokens[idx][1])):
                idx += 1
            expecting = True
            continue
        # `bash -c '<shell>'` — the operand is nested shell source
        if val in NESTED_SHELL:
            j = idx
            saw_c = False
            while j < len(tokens) and tokens[j][0] == "word":
                if tokens[j][1] == "-c":
                    saw_c = True
                    j += 1
                    break
                if not tokens[j][1].startswith("-"):
                    break
                j += 1
            if saw_c and j < len(tokens) and tokens[j][0] == "word":
                nested = scan(tokens[j][1], origin=f"{origin} (nested)")
                for c in nested.commands:
                    result.commands.append(Command(c.word, line, "nested shell -c"))
                result.unresolved.extend(nested.unresolved)
                idx = j + 1
        expecting = False

    # ------------------------------------------------------------------ GATE 4N-I28AV completeness
    # Measured, never assumed. ADV-I28AT-01 returned a partial result as COMPLETE because nothing
    # measured anything: the loop reached the last token and skipped every word, so by its own
    # reckoning it had succeeded. These figures come from the tokeniser and from balance counting,
    # so a skip of any kind is visible in the RESULT rather than only in its absence.
    result.source_length = len(src)
    result.start_position = 0
    result.consumed_position = min(tok.i, len(src))
    result.eof_reached = tok.i >= len(src)
    result.words_seen = sum(1 for k, _v, _l, _e in tokens if k == "word")
    if not result.eof_reached:
        result.unconsumed_ranges.append((result.consumed_position, len(src)))

    # Open frames. Counting keyword openers against their terminators catches a construct that was
    # entered and never closed — the shape a malformed `case` has, and the shape a future
    # early-return would have.
    words = [v for k, v, _l, _e in tokens if k == "word"]
    for opener, closer in (("case", "esac"), ("if", "fi")):
        depth = words.count(opener) - words.count(closer)
        if depth > 0:
            result.open_frames.extend([opener] * depth)
    loops = words.count("for") + words.count("while") + words.count("until") + words.count("select")
    if loops > words.count("done"):
        result.open_frames.extend(["loop"] * (loops - words.count("done")))
    if in_case_pattern:
        result.open_frames.append("case pattern")
    if case_awaiting_in:
        result.parse_errors.append(
            f"{case_awaiting_in} `case` construct(s) with no `in` keyword; real bash rejects this "
            "syntax, so the scan is malformed rather than complete")

    if result.open_frames or result.parse_errors:
        result.status = "MALFORMED"
    elif not result.eof_reached:
        result.status = "PARTIAL"
    elif result.unsupported:
        result.status = "UNSUPPORTED"
    elif result.unresolved:
        result.status = "COMPLETE_WITH_DECLARED_UNRESOLVED"
    else:
        result.status = "COMPLETE"
    return result


def scan_script(text: str, *, origin: str = "<script>") -> ScanResult:
    """A whole shell FILE, including its shebang interpreter."""
    result = scan(text, origin=origin)
    first = text.splitlines()[0] if text else ""
    if first.startswith("#!"):
        parts = first[2:].split()
        if parts:
            interp = parts[0].rsplit("/", 1)[-1]
            result.commands.append(Command(interp, 1, "shebang interpreter"))
            if interp == "env" and len(parts) > 1:
                result.commands.append(Command(parts[1], 1, "env interpreter"))
    return result


def local_functions(text: str) -> set:
    """Function names defined in this text; they are not external executables."""
    return set(re.findall(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{", text, re.M))


def main(argv=None) -> int:
    import argparse
    import json
    import sys
    from pathlib import Path
    ap = argparse.ArgumentParser(description="Command positions in a shell file.")
    ap.add_argument("path")
    args = ap.parse_args(argv)
    text = Path(args.path).read_text(encoding="utf-8")
    r = scan_script(text, origin=args.path)
    print(json.dumps({
        "commands": [{"word": c.word, "line": c.line, "construct": c.construct} for c in r.commands],
        "unresolved": [{"word": c.word, "line": c.line, "reason": c.reason} for c in r.unresolved],
        "executables": sorted(r.executables(local_functions(text))),
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --------------------------------------------------------------------------- session binding
def completeness_digest() -> dict:
    """A parse-completeness fingerprint of every tracked shell source (Gate 4N-I28AV).

    Bound into the session baseline and re-derived at session finish. ADV-I28AT-01 could not have
    been caught by comparing COMMANDS alone: the parse looked successful and simply contained
    fewer of them. What is bound here is the completeness EVIDENCE — status, consumed position, EOF
    and open frames — so a parser that starts terminating early mid-session is visible as drift
    rather than as a quietly smaller inventory.
    """
    import hashlib
    import json as _json
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    sources = {}
    for path in sorted((repo / "scripts").rglob("*.sh")):
        result = scan_script(path.read_text(encoding="utf-8"), origin=path.name)
        sources[str(path.relative_to(repo))] = {
            "status": result.status,
            "eof_reached": result.eof_reached,
            "consumed_position": result.consumed_position,
            "source_length": result.source_length,
            "open_frames": list(result.open_frames),
            "parse_errors": len(result.parse_errors),
            "command_count": len(result.commands),
            "trustworthy": result.is_trustworthy(),
            # GATE 4N-I28BB. Binding the transfer sites — not just a command count — is what makes a
            # dropped `exec` child visible as drift. A count alone would fall by one and look like
            # any other edit; the classification and child identity would not.
            "transfer_sites": [
                {"word": t.word, "line": t.line, "child": t.child,
                 "classification": t.classification, "options": list(t.options)}
                for t in result.transfer_sites
            ],
        }
    payload = _json.dumps(sources, sort_keys=True)
    return {"sources": sources,
            "grammar_version": "4N-I28AV.1",
            # GATE 4N-I28BB. Separate from grammar_version so a change to the exec model is
            # attributable to the exec model, and so the option table itself is bound: a widened
            # arity or a silently added option changes this digest.
            "exec_grammar_version": EXEC_GRAMMAR_VERSION,
            "exec_grammar_digest": hashlib.sha256(
                _json.dumps(exec_grammar_contract(), sort_keys=True).encode()).hexdigest(),
            "transfer_site_total": sum(len(v["transfer_sites"]) for v in sources.values()),
            "supported_forms": len(supported_forms()),
            "unsupported_forms": len(unsupported_forms()),
            "digest": hashlib.sha256(payload.encode()).hexdigest(),
            "untrustworthy": sorted(k for k, v in sources.items() if not v["trustworthy"])}
