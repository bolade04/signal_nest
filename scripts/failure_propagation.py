#!/usr/bin/env python3
"""Structural failure-propagation analysis — Gate 4N-I26B, closing I26B-03 (I25's ADV-Y2/Y3).

THE DEFECT THIS REPLACES. `ci_invocation_model.MASKING` was a four-string literal —
`("|| true", "||true", "|| :", "continue-on-error")`. `|| true` was caught because it was
spelled there; `|| echo 'suite non-blocking'` was not. Appended to the step that runs the entire
suite, it left pytest classified INVOKED (correctly — it IS invoked) and the model CLEAN, while
under `bash -e` the step exited 0 whatever the tests did.

    The model proved INVOCATION. It never proved that FAILURE FAILS THE JOB.

Those are different questions, and only the second is what a graded step is for.

WHY A LONGER LIST WOULD BE THE SAME DEFECT. The obvious repair is to add `|| echo`, `|| printf`,
`|| :` and so on. That control still fails the moment someone writes a form nobody listed —
`|| my_warn`, `|| { echo x; }`, a wrapper function that returns 0. Recognising bad forms means
the unrecognised form passes.

SO THIS INVERTS THE QUESTION. Instead of asking "does this line match a known masking form?",
it asks "is this command's non-zero exit GUARANTEED to reach the step's exit status?" — and
answers NO unless the structure proves otherwise. An `||` whose right-hand side this module
cannot prove is itself failing is MASKED. A pipeline without `pipefail` is MASKED for every
element but the last. `set +e` masks everything after it. An unrecognised construct is UNKNOWN,
and for a graded step UNKNOWN FAILS CLOSED.

That is the difference between a detector that must enumerate every way to hide a failure and
one that must be shown a way to propagate it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

PROPAGATES = "PROPAGATES"
MASKED = "MASKED"
UNKNOWN = "UNKNOWN"

# Commands whose exit status is always zero, so an `||` right-hand side made of one cannot
# re-raise the failure it caught. Used ONLY to classify a right-hand side as definitely
# masking; an unrecognised right-hand side is UNKNOWN, never PROPAGATES.
ALWAYS_SUCCEEDS = {"true", ":", "echo", "printf", "cat", "cd", "pwd", "export"}

# The one exception that genuinely re-raises: a right-hand side that exits non-zero.
_REEXIT = re.compile(r"\bexit\s+[1-9]|\bfalse\b|\breturn\s+[1-9]")

# GATE 4N-I27R. The inversion this module always CLAIMED, actually performed.
#
# WHAT WAS WRONG. classify_line() ended with `return PROPAGATES, "plain command under set -e"`.
# That is a FAIL-OPEN default: every construct the branches above did not recognise was
# certified safe. The module's own docstring says "an unrecognised construct is UNKNOWN and
# fails closed", and check() genuinely does fail closed on UNKNOWN — but classify_line almost
# never produced one, so the fail-closed machinery was starved. Gate 4N-I27Q's adversarial and
# security lanes both broke it with a trailing `&`, which bash runs in the background and whose
# list status is 0: `bash -euo pipefail -c 'false & echo done'` exits 0 while this module
# reported PROPAGATES. `set +o errexit` slipped past the `set +e` regex the same way.
#
# THE REPAIR. PROPAGATES is now returned only for a line PROVEN to be a simple command: one
# whose text contains no shell construct that can redirect, defer, group, background, capture
# or otherwise displace its exit status. Anything else falls to UNKNOWN, and for a graded step
# UNKNOWN is a finding. Adding `&` to a list of bad forms would have been the same defect one
# more time; the point is that the unrecognised form must now LOSE, not win.
#
# Quoted text is blanked before the structural test so that `echo "a; b"` is not mistaken for a
# compound command — a false finding trains people to ignore the guard, which is its own defect.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")

# Any of these can move a command's exit status somewhere the step never sees.
_STATUS_ALTERING = re.compile(r"[&;|(){}<>`]|\$\(")

# Backgrounding: a single `&` that is not `&&` and not a redirection like `2>&1` or `>&2`.
_BACKGROUND = re.compile(r"(?<![&>0-9])&(?!&)")

# GATE 4N-I27U. QUOTE AND COMMENT AWARENESS, applied BEFORE any control is recognised.
#
# WHAT WAS WRONG. Three of the four blockers Gate 4N-I27T's adversarial lane found were the
# same mistake wearing different clothes: a regex was matched against RAW LINE TEXT, so quoted
# prose acted as shell syntax. `|| echo "no false positives were suppressed"` classified
# PROPAGATES because `\bfalse\b` matched inside the message, and `echo "shifting << SWALLOW"`
# opened a heredoc that swallowed the rest of the step. The old `_QUOTED` regex could not fix
# this: `'[^']*'|"[^"]*"` cannot track state across a line, mis-handles escapes, and knows
# nothing about comments.
#
# THE REPAIR. One scanner walks the line once, tracking single-quote, double-quote and
# backslash state, and returns the line's CODE with every quoted span blanked to spaces and any
# unquoted `#` comment removed. Positions are preserved so offsets still line up. Every control
# recogniser below reads the CODE, never the raw text. Data cannot be syntax.
def shell_code(line: str) -> str:
    """The executable part of a line: quoted spans blanked, trailing comment removed.

    Blanked rather than deleted so that column positions still correspond to the original —
    a deletion would let `ec"h"o` collapse into a keyword that was never written.
    """
    out = []
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            out.append(" ")
            escaped = False
            continue
        if char == "\\" and quote != "'":
            # Inside single quotes a backslash is literal; everywhere else it escapes.
            out.append(" ")
            escaped = True
            continue
        if quote:
            out.append(" " if char != quote else char)
            if char == quote:
                quote = None
                out[-1] = " "
            continue
        if char in "'\"":
            quote = char
            out.append(" ")
            continue
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            break                      # an unquoted `#` at a word boundary starts a comment
        out.append(char)
    return "".join(out)


# GATE 4N-I27Z, AGENDA C. A BOUNDED LEXER, because blanking alone is a ONE-WAY defence.
#
# WHAT WAS WRONG. shell_code() blanks quoted spans so DATA cannot act as SYNTAX — the Gate
# 4N-I27U repair. Gate 4N-I27Y's adversarial lane showed the inverse now holds: SYNTAX can hide
# as DATA. `"set" +e`, `'set' +e`, `\set +e` and `s'e't +e` all run the `set` builtin and disable
# errexit — bash printed SURVIVED and exited 0 for every one — yet each blanks to something like
# `      +e`, so ERRMODE_OFF never matched and the module reported NONE.
#
# THE REPAIR. Bash performs QUOTE REMOVAL during word formation: quoting changes how a word is
# parsed, not what word it is. So words are built once, with quotes removed, and each word
# remembers whether any part of it was quoted. Control recognition then reads the WORD, while
# argument text stays inert because it is a different word: in `echo "remember set +e later"`
# the command word is `echo` and the prose is one argument — `set` is never a command word.
#
# This is a bounded lexer over the constructs the workflow and the corpus actually contain. It
# is deliberately NOT a shell parser: anything it cannot resolve is reported unresolved and the
# caller fails closed.
_WORD_SEPARATORS = ";&|()<>"


def shell_words(line: str) -> list[dict]:
    """Words after quote removal, each tagged with where a new command begins.

    Returns dicts: {"word", "quoted", "command_position", "dynamic"}. `dynamic` marks a word
    whose text cannot be known statically ($ or backtick outside single quotes), which callers
    must treat as unproven rather than assume benign.
    """
    words: list[dict] = []
    current, quoted, dynamic, has_text = [], False, False, False
    command_position = True
    quote = None
    escaped = False

    def flush():
        nonlocal current, quoted, dynamic, has_text, command_position
        if has_text:
            words.append({"word": "".join(current), "quoted": quoted,
                          "command_position": command_position, "dynamic": dynamic})
            command_position = False
        current, quoted, dynamic, has_text = [], False, False, False

    for index, char in enumerate(line):
        if escaped:
            current.append(char)
            has_text = True
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            quoted = True          # a backslash-escaped char is quoting, and `\set` is `set`
            has_text = True
            continue
        if quote:
            if char == quote:
                quote = None
            else:
                current.append(char)
                if quote == '"' and char in "$`":
                    dynamic = True
            continue
        if char in "'\"":
            quote = char
            quoted = True
            has_text = True
            continue
        if char == "#" and not has_text:
            break                                   # a comment starts a new word position
        if char.isspace():
            flush()
            continue
        if char in _WORD_SEPARATORS:
            flush()
            command_position = True                 # a new command begins after a separator
            continue
        if char in "$`":
            dynamic = True
        current.append(char)
        has_text = True
    flush()
    return words


def command_words(line: str) -> list[dict]:
    """Only the words that begin a command — where a keyword is a keyword."""
    return [w for w in shell_words(line) if w["command_position"]]


# Disabling / restoring errexit, in every spelling bash accepts, matched against shell_code().
#
# A `+` option CLUSTER containing `e` disables errexit — `set +e`, `set +ex`, `set +xe` all do.
#
# GATE 4N-I27U. `builtin` and `command` are ordinary prefixes that run the SAME `set` builtin:
# `builtin set +e` disables errexit exactly as `set +e` does (proven by bash, not assumed).
# Anchoring on the literal token `set` therefore missed two spellings that work. The prefix is
# now optional and repeatable, because `builtin command set +e` is also valid.
_ERRMODE_PREFIX = r"(?:(?:builtin|command)\s+)*"
ERRMODE_OFF = re.compile(
    rf"^\s*{_ERRMODE_PREFIX}set\s+(?:\+[A-Za-z]*e[A-Za-z]*|\+o\s+errexit\b)")
ERRMODE_ON = re.compile(
    rf"^\s*{_ERRMODE_PREFIX}set\s+(?:-[A-Za-z]*e[A-Za-z]*|-o\s+errexit\b)")

# `eval` runs its argument as shell. Handled CONSERVATIVELY and in exactly three cases:
#   * a fully static argument whose text disables errexit  -> disabled
#   * a fully static argument whose text restores errexit  -> restored
#   * anything else                                        -> UNPROVEN, and the caller fails closed
# Building a general shell interpreter is explicitly not the goal; refusing to guess is.
_EVAL = re.compile(r"^\s*" + _ERRMODE_PREFIX + r"eval\s+(?P<arg>.+)$")
_DYNAMIC = re.compile(r"[$`]")

ERRMODE_DISABLED, ERRMODE_RESTORED, ERRMODE_UNPROVEN, ERRMODE_NONE = (
    "DISABLED", "RESTORED", "UNPROVEN", "NONE")


_ERRMODE_WRAPPERS = ("builtin", "command")


# GATE 4N-I28I, ROOT CAUSE RC-1. ONE command-boundary model, shared by every recogniser.
#
# THE DEFECT THIS CLOSES — Gate 4N-I28G findings ARCH-01, ARCH-02, SEC-01 and ADV-02, four
# independent findings from three lanes. Both `trap_effect()` and `_errmode_from_words()` located
# their control word only at shell-word index 0, so a state change beginning a LATER command on the
# same line was invisible:
#
#     echo x; trap 'exit 0' ERR   bash exits 0 (absorbs)   module said NONE / PROPAGATES
#     echo x; set +e              bash exits 0 (errexit off) module said NONE / PROPAGATES
#     trap 'exit 1' ERR; trap 'exit 0' ERR   bash absorbs   module saw only the first trap
#
# Seven such forms disagreed with bash while the release policy asserted zero disagreements.
#
# The module already contradicted itself: `_TRAP_WORD` matches `trap` after any of `[;&|(){}\s]`
# and `trap_scope()` used it, so `trap_scope("echo x; trap 'exit 0' ERR")` returned PARENT while
# `trap_effect()` on the same string returned NONE. Two functions, one file, different answers
# about where a command may appear — and the fail-open one drove the state machine.
#
# The fix is the generalisation Gate 4N-I27Z Agenda A already performed for compound context:
# apply the EXISTING recognisers at every executable command boundary rather than only the first.
# No new regex vocabulary, and emphatically not a general Bash parser — an unsupported construct
# still resolves to UNPROVEN and fails closed.


def command_segments(raw: str) -> list[list[dict]]:
    """Split a line into executable command segments at command-position boundaries.

    `shell_words()` already marks the first word of each command; this groups on that mark, so
    `echo x; trap 'exit 0' ERR` yields [[echo, x], [trap, 'exit 0', ERR]].
    """
    segments: list[list[dict]] = []
    current: list[dict] = []
    for word in shell_words(raw):
        if word["command_position"] and current:
            segments.append(current)
            current = []
        current.append(word)
    if current:
        segments.append(current)
    return segments


def _scope_at(code: str, offset: int) -> str:
    """PARENT or SUBSHELL for a command starting at `offset` in blanked shell code.

    `( … )` is a child shell; `{ …; }` is not. Quoted spans are already blanked by shell_code(),
    so a parenthesis inside a string opens no scope.
    """
    prefix = code[:offset]
    return TRAP_SCOPE_SUBSHELL if prefix.count("(") > prefix.count(")") else TRAP_SCOPE_PARENT


def _segment_scopes(raw: str, head: str) -> list[str]:
    """Scope of each command whose head word is `head`, in order of appearance."""
    code = shell_code(raw)
    pattern = re.compile(r"(?:(?<=[;&|(){}\s])|^)" + re.escape(head) + r"(?=\s|$)")
    return [_scope_at(code, m.start()) for m in pattern.finditer(code)]


_ERRMODE_SKIPPABLE = frozenset(_ERRMODE_WRAPPERS) | {"{"}


def _errmode_from_segment(words: list[dict]) -> str | None:
    """The errexit opinion of ONE command segment. None = this segment says nothing."""
    if not words:
        return None
    index = 0
    # The command word may be preceded by any number of `builtin`/`command` wrappers, each of
    # which may itself be quoted: `builtin "set" +e` and `"builtin" set +e` both run `set`.
    # GATE 4N-I28I RC-1: a leading `{` is a GROUP opener, and a group runs in the CURRENT shell,
    # so `{ set +e; }` disables errexit exactly as a bare `set +e` does. The trap side already
    # skipped it via _TRAP_PREFIXES; the errexit side did not, which this corpus caught.
    while index < len(words) and words[index]["word"] in _ERRMODE_SKIPPABLE:
        if words[index]["dynamic"]:
            return ERRMODE_UNPROVEN
        index += 1
    if index >= len(words):
        return None
    head = words[index]
    # A dynamic COMMAND word means the command is unknown — that is classify_line's question,
    # not an error-mode question, and answering UNPROVEN here would flag every `$TOOL x.py`
    # line in the workflow. The genuine dynamic risk (`eval "$m"`) is handled by the eval path.
    if head["dynamic"] or head["word"] != "set":
        return None
    options = [w for w in words[index + 1:]]
    if any(w["dynamic"] for w in options):
        return ERRMODE_UNPROVEN
    text = " ".join(w["word"] for w in options)
    if re.match(r"^\+[A-Za-z]*e[A-Za-z]*(\s|$)|^\+o\s+errexit\b", text):
        return ERRMODE_DISABLED
    if re.match(r"^-[A-Za-z]*e[A-Za-z]*(\s|$)|^-o\s+errexit\b", text):
        return ERRMODE_RESTORED
    return None


def _errmode_from_words(raw: str) -> str | None:
    """Resolve `set` at EVERY command boundary, not only the first.

    GATE 4N-I28I RC-1. A `set` beginning a later command on the same line changes the shell just
    as much as one at the start, so every segment is asked. The LAST segment with an opinion wins,
    matching bash: a later `set` supersedes an earlier one. A `set` inside `( … )` is scoped to the
    child shell and does not change the parent's state.
    """
    scopes = _segment_scopes(raw, "set")
    seen = 0
    verdict: str | None = None
    for segment in command_segments(raw):
        opinion = _errmode_from_segment(segment)
        if opinion is None:
            continue
        scope = scopes[seen] if seen < len(scopes) else TRAP_SCOPE_PARENT
        seen += 1
        if scope == TRAP_SCOPE_SUBSHELL:
            continue                     # a child shell's errexit state dies with it
        verdict = opinion
    return verdict


# GATE 4N-I27Z, AGENDA B. ERR/EXIT TRAPS, which were not modelled at all.
#
# `trap 'exit 0' ERR` makes bash leave with status 0 the moment any command fails, so the step
# reports success while the guard genuinely failed — proven by execution, exit 0. Gate 4N-I27Y's
# adversarial lane found this: `grep -n '\btrap\b'` across the module returned only prose.
#
# The model is deliberately narrow. A trap whose body is statically knowable AND exits zero (or
# is a pure no-op) ABSORBS failures. A trap whose body is statically knowable and exits non-zero
# still replaces the status this module reasons about, so it is UNPROVEN rather than safe. A
# dynamic body is UNPROVEN. `trap - ERR` removes. Everything else is left alone.
TRAP_ABSORBS, TRAP_NONABSORBING, TRAP_REMOVED, TRAP_UNPROVEN, TRAP_NONE = (
    "ABSORBS", "NONABSORBING", "REMOVED", "UNPROVEN", "NONE")

TRAP_SCOPE_PARENT, TRAP_SCOPE_SUBSHELL = "PARENT", "SUBSHELL"

_TRAP_SIGNALS = {"ERR", "EXIT", "0"}

# GATE 4N-I28B, FINDING I28A-01. Bash decides which of these ends the script successfully, and
# only one of them does:
#
#     trap 'exit 0' ERR   -> exit 0    the trap exits, with status 0
#     trap 'exit 1' ERR   -> exit 1    the trap exits, with a failing status
#     trap 'exit'   ERR   -> exit 7    bare `exit` REUSES the current status, which is the failure
#     trap ':'      ERR   -> exit 7    the body runs and returns 0, then errexit still terminates
#     trap 'true'   ERR   -> exit 7    likewise; returning 0 is not the same as EXITING with 0
#     trap 'true'   EXIT  -> exit 7    an EXIT trap that does not exit cannot change the status
#
# The previous pattern lumped bare `exit`, `:` and `true` in with `exit 0`, which is how three of
# them came to be called absorbing. Running successfully and TERMINATING THE SHELL successfully
# are different events.
_TRAP_EXPLICIT_SUCCESS = re.compile(r"^\s*exit\s+0+\s*;?\s*$")
_TRAP_EXPLICIT_FAILURE = re.compile(r"^\s*exit\s+(?!0+\s*;?\s*$)\d+\s*;?\s*$")
_TRAP_STATUS_PRESERVING = re.compile(r"^\s*(?:exit|:|true)\s*;?\s*$")

# `{ ...; }` is a GROUP: it runs in the CURRENT shell. `( ... )` is a SUBSHELL: it runs in a
# child, so anything it installs dies with that child.
_TRAP_PREFIXES = frozenset(_ERRMODE_WRAPPERS) | {"{"}
_TRAP_WORD = re.compile(r"(?:(?<=[;&|(){}\s])|^)trap(?=\s|$)")


def trap_scope(raw: str) -> str:
    """Which shell a trap on this line installs into.

    GATE 4N-I28B. `( trap 'exit 0' ERR )` does NOT bind the parent — bash exits 7 on a later
    failure, because the trap died with the subshell. `{ trap 'exit 0' ERR; }` DOES bind it —
    bash exits 0 — because a brace group is not a new shell. Both were wrong before: the first
    was called absorbing, and the second was not seen at all.

    Parenthesis depth is counted in shell_code(), where quoted spans are already blanked, so a
    `(` inside a string cannot open a scope.
    """
    code = shell_code(raw)
    match = _TRAP_WORD.search(code)
    if match is None:
        return TRAP_SCOPE_PARENT
    prefix = code[:match.start()]
    return TRAP_SCOPE_SUBSHELL if prefix.count("(") > prefix.count(")") else TRAP_SCOPE_PARENT


def trap_effect(raw: str) -> str:
    """What this line does to the ERR/EXIT trap.

    ABSORBS is reserved for a trap that is PROVEN to apply in the parent shell and to terminate
    it with an explicit zero status. Everything statically knowable but non-absorbing is
    NONABSORBING; everything else is UNPROVEN, which fails closed on a graded step.
    """
    scopes = _segment_scopes(raw, "trap")
    seen = 0
    verdict = TRAP_NONE
    for segment in command_segments(raw):
        opinion = _trap_from_segment(segment)
        if opinion is TRAP_NONE:
            continue
        scope = scopes[seen] if seen < len(scopes) else TRAP_SCOPE_PARENT
        seen += 1
        if scope == TRAP_SCOPE_SUBSHELL:
            # Installed in a child shell: it cannot absorb anything the parent does later, and it
            # does not replace the parent's trap either.
            verdict = TRAP_NONABSORBING if verdict is TRAP_NONE else verdict
            continue
        verdict = opinion                # a later trap on the same signal replaces the earlier one
    return verdict


def _trap_from_segment(words: list[dict]) -> str:
    """The trap opinion of ONE command segment, ignoring scope (the caller applies that)."""
    index = 0
    while index < len(words) and words[index]["word"] in _TRAP_PREFIXES:
        index += 1
    if index >= len(words) or words[index]["word"] != "trap":
        return TRAP_NONE
    rest = words[index + 1:]
    if not rest:
        return TRAP_NONE
    body = rest[0]

    # The signal list belongs to THIS trap. A following command (`trap ... ERR; echo hi`) starts
    # at the next command-position word and its arguments are not signals.
    arguments = []
    for word in rest[1:]:
        if word["command_position"]:
            break
        arguments.append(word)
    signals = {w["word"].upper() for w in arguments}
    if not (signals & _TRAP_SIGNALS):
        return TRAP_NONE                 # a trap on some other signal does not affect exit status

    if body["word"] == "-":
        return TRAP_REMOVED
    if body["dynamic"]:
        return TRAP_UNPROVEN
    if _TRAP_EXPLICIT_SUCCESS.match(body["word"]):
        return TRAP_ABSORBS
    if _TRAP_EXPLICIT_FAILURE.match(body["word"]) or _TRAP_STATUS_PRESERVING.match(body["word"]):
        return TRAP_NONABSORBING
    # A statically knowable body that does something else still substitutes for the failing
    # command's status. This module does not model what it substitutes, so it refuses.
    return TRAP_UNPROVEN


def errmode_effect(raw: str) -> str:
    """What this line does to errexit: DISABLED, RESTORED, UNPROVEN, or NONE.

    UNPROVEN is not a synonym for NONE. It means the line MIGHT change the mode and this module
    cannot prove it does not — which for a graded step must fail closed, exactly as an
    unrecognised command does.
    """
    # GATE 4N-I27Z, AGENDA C. Recognition now reads WORDS with quote removal applied, so a
    # quoted or escaped spelling of `set` is still the `set` builtin. The old text match against
    # shell_code() is kept as a second path because it still catches everything it used to;
    # neither path can promote prose, because prose is never a command word.
    resolved = _errmode_from_words(raw)
    if resolved is not None:
        return resolved

    code = shell_code(raw)
    if ERRMODE_OFF.match(code):
        return ERRMODE_DISABLED
    if ERRMODE_ON.match(code):
        return ERRMODE_RESTORED

    match = _EVAL.match(code)
    if not match:
        # An `eval` hidden behind quoting is not an eval; shell_code already proved that.
        return ERRMODE_NONE

    # The argument text as WRITTEN, so a static quoted string can be read back out.
    raw_arg = raw[raw.index("eval") + len("eval"):].strip() if "eval" in raw else ""
    literal = _static_eval_argument(raw_arg)
    if literal is None:
        return ERRMODE_UNPROVEN
    if ERRMODE_OFF.match(shell_code(literal)):
        return ERRMODE_DISABLED
    if ERRMODE_ON.match(shell_code(literal)):
        return ERRMODE_RESTORED
    return ERRMODE_NONE


def _static_eval_argument(arg: str) -> str | None:
    """The literal string an `eval` will run, or None when it cannot be known statically."""
    arg = arg.strip()
    if not arg:
        return None
    if _DYNAMIC.search(arg):
        return None                     # $var, $(...) or `...` — the text is not knowable here
    if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "'\"":
        inner = arg[1:-1]
        return None if _DYNAMIC.search(inner) else inner
    return arg                          # an unquoted, undollared word list is its own text

# `wait "$!"` (or `wait $!`) is the one construct that recovers a backgrounded job's status.
WAIT_FOR_LAST = re.compile(r"^\s*wait\s+[\"']?\$!")

# The opening of a heredoc, whose BODY is data rather than commands.
#
# GATE 4N-I27U. This pattern used to be matched against RAW TEXT, and searched rather than
# anchored to a redirection position. `echo "shifting << SWALLOW"` therefore opened a heredoc
# with the tag SWALLOW, and because no line ever equalled SWALLOW the rest of that graded step
# was skipped UNCLASSIFIED — a general blinding primitive that also concealed a following
# `set +e`, since the error-mode check sat inside the skipped region.
#
# That defect was a DIRECT CONSEQUENCE of the Gate 4N-I27R repair, which moved heredoc
# substitution BEFORE quote-stripping so a real opener like `<<'PY'` would still be seen. Both
# properties have to hold at once, and the fix is not to reorder two regexes but to stop
# looking at quoted text at all: openers are now found in shell_code(), where quoted spans are
# already blank. A real `<<'PY'` survives because its `<<` is code even though its tag is
# quoted, and the tag is recovered from the RAW line at the offset the code match reports.
#
# `<<<` is a here-STRING, not a here-document: it has no body and no terminator, so treating it
# as an opener would swallow the remainder of the step. It is excluded explicitly.
_HEREDOC_OPEN = re.compile(r"<<(?!<)(?P<dash>-?)\s*(?P<tag>[\"']?[A-Za-z_][A-Za-z0-9_]*[\"']?)")

# The OPERATOR alone. Located in shell_code() to prove it is a real redirection; the TAG is then
# read from the raw line, because `<<'PY'` quotes its own tag and shell_code has blanked it.
# Matching the tag in code would silently stop recognising every quoted-delimiter heredoc — a
# regression that turns inline Python bodies back into "shell" and floods the graded steps with
# false UNKNOWNs. Operator in code, tag in raw: both halves of Agenda D at once.
# `(?<!<)` as well as `(?!<)`: without the look-BEHIND, `<<<` matches at its second character
# and a here-STRING is read as a here-document, swallowing the rest of the step. Found by this
# gate's own corpus, not by reasoning about the pattern.
_HEREDOC_OPERATOR = re.compile(r"(?<!<)<<(?!<)(?P<dash>-?)")

# GATE 4N-I27W. Bash suspends errexit for the command being TESTED. Proven, not assumed:
# `if false; then ...`, `while false; do ...` and `! false` each exit 0 under -euo pipefail.
# A line carrying any of this syntax is not a plain sequence, and this module refuses to model
# it rather than pretending a sequence rule applies.
_COMPOUND_KEYWORDS = frozenset({
    "if", "elif", "then", "else", "fi", "while", "until", "do", "done", "for", "case",
    "esac", "select", "!"})

# Separators after which a new COMMAND begins. A shell keyword is only a keyword in command
# position; anywhere else it is an ordinary word.
_COMMAND_SEPARATOR = re.compile(r"(?:;|&&|\|\||\||\(|\{|^)")


def _in_compound_context(code: str) -> bool:
    """True when a compound/conditional keyword appears in COMMAND POSITION.

    GATE 4N-I27W. A first version of this test matched the keyword anywhere on the line, and
    `python3 -c "..."; echo done` was read as compound syntax because the ARGUMENT `done` looked
    like a keyword. That is the very defect this gate was chartered to audit for — a decision
    taken from substring resemblance instead of parsed position — and it was caught by this
    gate's own corpus rather than by inspection. Keywords are now recognised only where a
    command may start.

    GATE 4N-I27Z. Kept for the `code`-string callers, but the authoritative test is now
    in_compound_context() below, which reads WORDS and is applied to EVERY line rather than only
    to lines carrying a `;`.
    """
    for segment in _COMMAND_SEPARATOR.split(code):
        words = segment.split()
        if words and words[0] in _COMPOUND_KEYWORDS:
            return True
    return False


def in_compound_context(raw: str) -> bool:
    """True when this line opens or continues a compound/conditional construct.

    GATE 4N-I27Z, AGENDA A. THE DEFECT THIS FIXES. `_in_compound_context()` had exactly ONE call
    site — inside the `";" in code` branch — so the entire compound model was unreachable for any
    line that carried no semicolon. Gate 4N-I27Y's architect, security and adversarial lanes each
    found it independently, and bash settles it:

        ! false                     -> exit 0   analyser said PROPAGATES
        if CMD / then / fi          -> exit 0   analyser said PROPAGATES
        while CMD / do / done       -> exit 0   analyser said PROPAGATES
        if CMD | cat; then ...      -> exit 0   analyser said PROPAGATES

    Bash suspends errexit for the command being TESTED, so the failure never reaches the step.
    A positive propagation claim there is a fail-open answer in the mandatory release control.

    The keyword is recognised only in COMMAND POSITION and only after quote removal, so an
    argument (`echo done`) and quoted prose (`echo "if you fail"`) both stay inert — the Gate
    4N-I27W property, preserved.
    """
    return any(w["word"] in _COMPOUND_KEYWORDS for w in command_words(raw))


def _split_on_code(raw: str, code: str, sep: str) -> list[str]:
    """Split RAW at the positions where SEP appears in CODE.

    Splitting the raw text directly would cut on a `;` inside a quoted string or a comment;
    splitting the code would lose the original characters. Using code POSITIONS against raw
    CONTENT gives both: only real separators split, and each segment keeps its own quoting.
    """
    cuts = [i for i, ch in enumerate(code) if ch == sep]
    segments, previous = [], 0
    for cut in cuts:
        segments.append(raw[previous:cut])
        previous = cut + 1
    segments.append(raw[previous:])
    return segments


def heredoc_opener(raw: str) -> dict | None:
    """The heredoc this line opens, or None. The OPERATOR must be code, never quoted text."""
    code = shell_code(raw)
    operator = _HEREDOC_OPERATOR.search(code)
    if not operator:
        return None
    match = _HEREDOC_OPEN.match(raw, operator.start())
    if not match:
        return None
    tag = match.group("tag").strip().strip("\"'")
    return {"tag": tag, "strip_tabs": match.group("dash") == "-"} if tag else None


def _structurally_simple(stripped: str) -> bool:
    """True only when the line is a plain command whose status is its own.

    Deliberately conservative: this answers "can I PROVE nothing displaces the status?", and
    any character that could is enough to answer no.
    """
    # A heredoc redirect feeds stdin; it does not displace the command's exit status, so
    # `python3 - "$work" <<'PY'` is still a simple command whose status is python3's.
    #
    # GATE 4N-I27U. shell_code() blanks quoted spans FIRST, which removes the ordering trap the
    # I27R version had to work around: there is no longer a "strip heredocs before quotes or
    # after" question, because by the time the heredoc operator is removed the quotes are
    # already gone and only real code remains.
    # The OPERATOR is removed, not the operator-plus-tag: shell_code has already blanked a
    # quoted tag like `'PY'`, so a pattern requiring the tag would no longer match and the bare
    # `<<` would be read as a status-altering redirection. That is how `python3 - <<'PY'` —
    # a plain command in every real sense — turned UNKNOWN mid-remediation.
    bare = _HEREDOC_OPERATOR.sub("", shell_code(stripped))
    return not _STATUS_ALTERING.search(bare)


class PropagationError(RuntimeError):
    """Fail-closed."""


def shell_contract(shell: str) -> dict:
    """The option state a shell invocation establishes, or a refusal to guess.

    GATE 4N-I27U. The old reading tested `"-e" in shell`, a substring test that is true for
    `--noprofile` and for any path containing `-e`. Options are now read as OPTIONS: bundled
    short flags (`-euo`), separate flags (`-e -u`), and `-o errexit` all count, and nothing
    else does.
    """
    if not shell or not shell.strip():
        return {"shell": shell, "determined": False,
                "why": "no shell is defined at any level, so its option state is unknown"}

    tokens = shell.split()
    errexit = pipefail = nounset = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-o" and index + 1 < len(tokens):
            option = tokens[index + 1]
            errexit |= option == "errexit"
            pipefail |= option == "pipefail"
            nounset |= option == "nounset"
            index += 2
            continue
        if token.startswith("-") and not token.startswith("--") and len(token) > 1:
            letters = token[1:]
            if letters.isalpha():
                errexit |= "e" in letters
                nounset |= "u" in letters
                # A cluster ENDING in `o` takes the next word as the option name: `-euo
                # pipefail` is `-e -u -o pipefail`. Missing this dropped pipefail on the
                # repository's own workflow, which would have re-judged every pipeline.
                if letters.endswith("o") and index + 1 < len(tokens):
                    option = tokens[index + 1]
                    errexit |= option == "errexit"
                    pipefail |= option == "pipefail"
                    nounset |= option == "nounset"
                    index += 2
                    continue
        index += 1

    interpreter = tokens[0] if tokens else ""
    if Path(interpreter).name not in ("bash", "sh", "zsh", "dash"):
        # A shell this module has no semantic model for. Refusing is the honest answer.
        return {"shell": shell, "determined": False,
                "why": f"unmodelled shell {interpreter!r}; its error-mode semantics are not known"}

    return {"shell": shell, "determined": True, "errexit": errexit,
            "pipefail": pipefail, "nounset": nounset}


def _shell_options(workflow_text: str) -> dict:
    """The workflow-level default shell. Job and step overrides are resolved in analyse()."""
    match = re.search(r"^defaults:\s*\n\s*run:\s*\n\s*shell:\s*(.+)$", workflow_text, re.M)
    shell = match.group(1).strip() if match else ""
    contract = shell_contract(shell)
    return {"shell": shell,
            "determined": contract["determined"],
            "why": contract.get("why"),
            "errexit": contract.get("errexit", False),
            "pipefail": contract.get("pipefail", False),
            "nounset": contract.get("nounset", False)}


def effective_shell(workflow_defaults: str, job: dict, step: dict) -> dict:
    """The shell that actually runs a step: step override, else job default, else workflow.

    GitHub resolves these three levels in exactly this order, and only the winner's options
    apply — a step-level `shell: bash` does NOT inherit the workflow's `-euo pipefail`. Reading
    only the workflow default therefore attributed errexit to steps that do not have it.
    """
    step_shell = step.get("shell")
    job_shell = ((job.get("defaults") or {}).get("run") or {}).get("shell")
    for level, shell in (("step", step_shell), ("job", job_shell),
                         ("workflow", workflow_defaults)):
        if shell:
            return {"level": level, **shell_contract(str(shell))}
    return {"level": "none", "shell": "", "determined": False,
            "why": "no shell is defined at step, job or workflow level"}


def classify_line(line: str, *, pipefail: bool, set_e_disabled: bool) -> dict:
    """Does a non-zero exit from this line reach the step's exit status?"""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return {"verdict": PROPAGATES, "why": "not a command"}

    # GATE 4N-I27Z, AGENDA A. FIRST, and independent of every separator.
    #
    # Compound recognition used to live inside the `;` branch, which meant a construct without a
    # semicolon was never tested and fell through to the plain-command exit. It is now the first
    # question asked of every line, before backgrounding, `||`, pipelines or sequences, because
    # each of those branches reasons about ORDINARY errexit semantics and a compound context is
    # precisely where those semantics do not apply.
    if in_compound_context(stripped):
        return {"verdict": UNKNOWN,
                "why": "the line opens or continues a compound/conditional construct, where "
                       "bash suspends errexit for the command being tested; propagation is NOT "
                       "established"}

    if set_e_disabled:
        return {"verdict": MASKED,
                "why": "errexit is disabled (`set +e` / `set +o errexit`), so a non-zero exit "
                       "does not stop the step"}

    # GATE 4N-I27R. Backgrounding, checked BEFORE any other structure: `cmd &` returns control
    # immediately and the list's status is 0, so the command's failure never reaches the step.
    # Executed proof: bash -euo pipefail -c 'false & echo done' exits 0.
    if _BACKGROUND.search(shell_code(stripped)):
        return {"verdict": MASKED,
                "why": "the command is BACKGROUNDED with `&`, so the list's status is 0 and "
                       "the job's real exit status never reaches the step"}

    # `cmd || rhs` — the failure is caught. It re-raises ONLY if rhs itself fails.
    #
    # GATE 4N-I27U. Both the `||` split and the re-raise test now read shell_code(), so a
    # message that merely CONTAINS the word `false` or the characters `exit 1` cannot promote a
    # masking line to PROPAGATES. Bash proves the point: `false || echo "no false positives
    # were suppressed"` exits 0. The head is still taken from the raw text, because that is the
    # command NAME and quoting does not change which command runs.
    code = shell_code(stripped)
    if "||" in code:
        split_at = code.index("||")
        rhs = stripped[split_at + 2:].strip()
        rhs_code = code[split_at + 2:].strip()
        head = re.split(r"[\s;&|]", rhs.lstrip("{( "), 1)[0]
        if _REEXIT.search(rhs_code):
            return {"verdict": PROPAGATES,
                    "why": f"`|| {rhs[:40]}` re-raises with a non-zero exit"}
        if head in ALWAYS_SUCCEEDS or not head:
            return {"verdict": MASKED,
                    "why": f"`|| {rhs[:40]}` always succeeds, so the left-hand failure is "
                           "swallowed and the step exits 0"}
        return {"verdict": UNKNOWN,
                "why": f"`|| {rhs[:40]}` — this module cannot prove the right-hand side fails, "
                       "so propagation is NOT established"}

    # A pipeline reports only its LAST element unless pipefail is set.
    if "|" in code and not re.search(r"\|\|", code):
        if not pipefail:
            return {"verdict": MASKED,
                    "why": "pipeline without `pipefail`: only the last element's status is "
                           "reported, so a failure upstream of the pipe is lost"}
        return {"verdict": PROPAGATES, "why": "pipeline under `pipefail`"}

    # `cmd; true` and `cmd && x; true` — a trailing always-succeeding command becomes the status.
    # GATE 4N-I27W, FINDING I27V-01. A `;` SEQUENCE IS NOT ONE VERDICT.
    #
    # WHAT WAS WRONG. This branch returned MASKED for `cmd; true` whenever the trailing command
    # always succeeds — WITHOUT consulting the errexit state that was already sitting in
    # set_e_disabled. Bash settles it: `false; true` exits 1 under `set -e`, because `;`
    # separates two commands and errexit fires on the FIRST one; the trailing `true` is never
    # reached. Without `-e` the same line exits 0. One construct, two behaviours, and the
    # analyser asserted one of them unconditionally.
    #
    # This is the SAME SHAPE as Gate 4N-I27U's Agenda A: a modelled state that exists and is
    # not consulted. That gate fixed it in the step loop and in the error-mode recogniser; this
    # branch was missed, and Gate 4N-I27V caught it by comparing the stored expectation against
    # bash rather than against the analyser.
    if ";" in code:
        # `-e` is SUSPENDED inside a condition or loop test: `if false; then ...` exits 0, as do
        # `while false; do ...` and `! false`. A line carrying compound syntax is therefore not
        # a plain sequence and this module does not model it — UNKNOWN, which fails closed.
        if _in_compound_context(code):
            return {"verdict": UNKNOWN,
                    "why": "the line carries compound/conditional syntax, where errexit is "
                           "suspended for the tested command; propagation is NOT established"}

        segments = _split_on_code(stripped, code, ";")
        last = segments[-1].strip()
        head = re.split(r"[\s;&|]", last, 1)[0] if last else ""

        if set_e_disabled:
            # DEFENSIVE, AND CURRENTLY UNREACHABLE. The early return at the top of this
            # function already answers MASKED for every line while errexit is disabled, so
            # control never arrives here with set_e_disabled true today. It is written out
            # anyway because the defect being fixed was precisely a branch that asserted an
            # errexit-OFF conclusion from inside the errexit-ON-only region: stating both
            # states makes this branch correct on its own terms rather than correct only
            # because of an invariant held forty lines away.
            #
            # Execution really does reach the trailing command, so its status is the line's.
            if last and head in ALWAYS_SUCCEEDS and not _REEXIT.search(shell_code(last)):
                return {"verdict": MASKED,
                        "why": f"errexit is disabled, so execution reaches `{last[:30]}`, "
                               "which always succeeds and becomes the line's exit status"}
            return {"verdict": UNKNOWN,
                    "why": "errexit is disabled and this module cannot prove which command "
                           "supplies the line's exit status; propagation is NOT established"}

        # errexit ENABLED. Each `;`-separated command runs in sequence: the first failure exits
        # the step immediately, and if none fails the last command's status is the line's. A
        # failure anywhere therefore reaches the step — PROVIDED every segment is a plain
        # command, so that nothing displaces a status. Otherwise UNKNOWN.
        if all(_structurally_simple(seg.strip()) for seg in segments if seg.strip()):
            return {"verdict": PROPAGATES,
                    "why": "under `set -e` a `;` sequence stops at the first failing command, "
                           "so a failure in any element reaches the step's exit status"}
        return {"verdict": UNKNOWN,
                "why": "a `;` sequence element carries a construct that can displace its exit "
                       "status; propagation is NOT established"}

    # `if ! cmd; then echo ...; fi` — the failure is absorbed by the conditional.
    if re.match(r"if\s+!", stripped):
        return {"verdict": MASKED,
                "why": "the command's failure is consumed as a CONDITION; unless the branch "
                       "exits non-zero the step continues successfully"}

    # ASSIGNMENT FROM A COMMAND SUBSTITUTION. The bash rule is exact and worth encoding rather
    # than approximating, because both halves of it are load-bearing:
    #
    #   var=$(cmd)          the assignment's status IS cmd's, so `set -e` stops the step
    #   local var=$(cmd)    the status is LOCAL's — always 0 — and cmd's failure is LOST
    #   export var=$(cmd)   same, for the same reason
    #
    # The second form is a classic silent-failure source. Treating both alike would either
    # raise a false finding on every correct assignment (noise that trains people to ignore the
    # guard) or miss the one that genuinely masks.
    if re.match(r"^(local|export|declare|readonly|typeset)\s+\w+=.*\$\(", stripped):
        head = stripped.split()[0]
        return {"verdict": MASKED,
                "why": f"`{head} var=$(cmd)` takes {head}'s exit status, which is always 0, so "
                       "the substituted command's failure is discarded"}
    if re.match(r'^\w+="?\$\([^)]*\)"?$', stripped):
        return {"verdict": PROPAGATES,
                "why": "a simple assignment takes the command substitution's exit status, so "
                       "`set -e` stops the step"}
    if re.match(r"\w+=\$\(", stripped) or "$?" in stripped:
        return {"verdict": UNKNOWN,
                "why": "the exit status is captured into a variable; propagation depends on "
                       "what is done with it and is not established here"}

    # `echo $(cmd)` — the line's status is echo's, not cmd's. Found by this gate's own corpus
    # run, which is the point of running the corpus rather than reasoning about the classifier.
    substitution = re.search(r"\$\(([^)]+)\)", stripped)
    if substitution:
        outer = re.split(r"[\s;&|]", stripped, 1)[0]
        if outer in ALWAYS_SUCCEEDS:
            return {"verdict": MASKED,
                    "why": f"`{outer}` always succeeds, so the status of the substituted "
                           f"`{substitution.group(1)[:30]}` is discarded"}
        return {"verdict": UNKNOWN,
                "why": "a command substitution's exit status is not the line's exit status "
                       "unless the outer command propagates it; not established here"}

    # GATE 4N-I27R. THE INVERSION. Previously this returned PROPAGATES unconditionally, which
    # made every unrecognised construct safe by default. Propagation must now be PROVEN.
    if _structurally_simple(stripped):
        return {"verdict": PROPAGATES, "why": "plain command under `set -e`"}
    return {"verdict": UNKNOWN,
            "why": "the line carries a shell construct that can displace its exit status and "
                   "this module cannot prove the status reaches the step; propagation is NOT "
                   "established"}


def analyse() -> dict:
    """Every graded step, every command line, classified."""
    try:
        import yaml
    except ModuleNotFoundError as exc:                       # pragma: no cover
        raise PropagationError("PyYAML is required for structural analysis") from exc

    text = WORKFLOW.read_text(encoding="utf-8")
    options = _shell_options(text)
    doc = yaml.safe_load(text)

    # GATE 4N-I27R. EVERY step is analysed, not only the ones carrying an `id:`.
    #
    # WHAT WAS WRONG. `if not step.get("id"): continue` silently dropped 49 of the workflow's 93
    # steps, so "masked 0" was a statement about 44 steps presented as a statement about the
    # workflow — and one of the dropped steps already contains a live `|| true`. A control that
    # decides its own scope by skipping is the same shape as a list that licenses itself.
    # Steps are now classified as GRADED (an `id:` whose outcome the guard aggregator reads) or
    # NON-GRADED; both are analysed and reported, and only the graded set fails the job, so the
    # release-policy scope stays honest without pretending the other 49 were examined.
    steps = []
    for job_name, job in (doc.get("jobs") or {}).items():
        for index, step in enumerate(job.get("steps") or []):
            sid = step.get("id")
            run = step.get("run") or ""
            outcome_read = bool(sid) and f'steps.{sid}.outcome' in text

            # GATE 4N-I27U, AGENDA A. The initial error-mode state is now DERIVED FROM THE
            # SHELL THAT ACTUALLY RUNS THIS STEP, not hardcoded to "enabled". The previous
            # `errexit_disabled = False` discarded the value _shell_options had just computed,
            # so deleting `-e` from defaults.run.shell changed real bash behaviour (exit 3
            # becomes exit 0) while every line kept reporting "plain command under `set -e`".
            # When the shell cannot be modelled the state is UNPROVEN and every line in the
            # step fails closed rather than defaulting to the safe answer.
            shell = effective_shell(options["shell"], job, step)
            errexit_disabled = not shell.get("errexit", False)
            errexit_unproven = not shell.get("determined", False)
            trap_absorbing = False          # AGENDA B: no ERR/EXIT trap is installed at entry
            trap_unproven = False
            pipefail = shell.get("pipefail", False)

            lines = []
            raw_lines = run.splitlines()
            heredoc = None
            for position, raw in enumerate(raw_lines):
                # A heredoc BODY is data, not commands. Without this the inverted classifier
                # reads inline Python as shell and reports it UNKNOWN — a false finding, which
                # is its own kind of defect. The command that OPENS the heredoc is still
                # classified normally on its own line.
                if heredoc is not None:
                    candidate = raw.lstrip("\t") if heredoc["strip_tabs"] else raw
                    if candidate.strip() == heredoc["tag"]:
                        heredoc = None       # analysis resumes on the NEXT line, as bash does
                    continue

                # AGENDA D. Openers are recognised in shell_code(), so a quoted or commented
                # `<<` is inert — but a real `<<'PY'` is still found.
                heredoc = heredoc_opener(raw)

                # AGENDA C. Error-mode changes are recognised in shell_code() too, with
                # `builtin`/`command` prefixes and a conservative `eval`.
                effect = errmode_effect(raw)
                if effect == ERRMODE_DISABLED:
                    errexit_disabled = True
                elif effect == ERRMODE_RESTORED:
                    errexit_disabled = False
                    errexit_unproven = False
                elif effect == ERRMODE_UNPROVEN:
                    errexit_unproven = True

                # AGENDA B. Trap state is carried exactly like errexit state. An absorbing ERR
                # trap makes every later failure invisible to the step; an unprovable one makes
                # every later line unprovable. Removal restores ordinary reasoning.
                #
                # GATE 4N-I28B: installing a trap REPLACES whatever was on that signal. Bash
                # confirms it — `trap 'exit 0' ERR` followed by `trap 'exit 1' ERR` exits 1, not
                # 0 — so a later non-absorbing trap must clear an earlier absorbing one exactly
                # as removal does.
                # A trap installed in a SUBSHELL changes nothing in the parent — not even an
                # earlier absorbing trap. Bash: `trap 'exit 0' ERR` then `( trap 'exit 1' ERR )`
                # still exits 0, while the same replacement in a `{ ...; }` group exits 1.
                trap = trap_effect(raw)
                if trap_scope(raw) == TRAP_SCOPE_SUBSHELL:
                    trap = TRAP_NONE
                if trap == TRAP_ABSORBS:
                    trap_absorbing = True
                    trap_unproven = False
                elif trap in (TRAP_REMOVED, TRAP_NONABSORBING):
                    trap_absorbing = False
                    trap_unproven = False
                elif trap == TRAP_UNPROVEN:
                    trap_absorbing = False
                    trap_unproven = True

                if trap_absorbing and trap == TRAP_NONE:
                    verdict = {"verdict": MASKED,
                               "why": "an ERR/EXIT trap that exits zero is installed, so a "
                                      "non-zero exit is replaced by success and never reaches "
                                      "the step"}
                elif trap_unproven and trap == TRAP_NONE:
                    verdict = {"verdict": UNKNOWN,
                               "why": "an ERR/EXIT trap with an unprovable body is installed; "
                                      "what it does to the exit status is NOT established"}
                elif errexit_unproven:
                    verdict = {"verdict": UNKNOWN,
                               "why": "the effective errexit state is not provable here "
                                      f"({shell.get('why') or 'dynamic `eval` may change it'}), "
                                      "so propagation is NOT established"}
                else:
                    verdict = classify_line(raw, pipefail=pipefail,
                                            set_e_disabled=errexit_disabled)
                # A backgrounded job IS recovered when the very next command waits on it and
                # that wait itself propagates. Nothing else rescues it.
                if verdict["verdict"] == MASKED and "BACKGROUNDED" in verdict["why"]:
                    following = next((l for l in raw_lines[position + 1:] if l.strip()), "")
                    if WAIT_FOR_LAST.match(following):
                        verdict = {"verdict": PROPAGATES,
                                   "why": "backgrounded, but the next command is `wait \"$!\"`, "
                                          "which returns the job's own exit status"}
                if verdict["why"] != "not a command":
                    lines.append({"line": raw.strip()[:120], **verdict})

            # AGENDA D, fail-closed tail. A heredoc still open at the end of the step was never
            # terminated: bash would treat the rest of the script as body, and every line this
            # loop skipped went UNCLASSIFIED. Silence there is precisely the blinding the
            # I27T adversarial lane weaponised, so an unterminated heredoc is a finding.
            if heredoc is not None:
                lines.append({
                    "line": f"<<{heredoc['tag']} (unterminated)",
                    "verdict": UNKNOWN,
                    "why": f"a heredoc opened with `{heredoc['tag']}` is never terminated, so "
                           "the remaining lines of this step were not classified and "
                           "propagation is NOT established"})
            steps.append({
                "id": sid or f"{job_name}#{index}:{step.get('name') or step.get('uses') or ''}",
                "job": job_name,
                "has_id": bool(sid),
                "graded": bool(sid) and outcome_read,
                "outcome_read_by_aggregator": outcome_read,
                "continue_on_error": bool(step.get("continue-on-error")),
                "lines": lines,
                "masked": [l for l in lines if l["verdict"] == MASKED],
                "unknown": [l for l in lines if l["verdict"] == UNKNOWN],
            })
    return {"shell": options, "steps": steps}


def check() -> dict:
    result = analyse()
    graded = [s for s in result["steps"] if s["graded"]]
    non_graded = [s for s in result["steps"] if not s["graded"]]
    problems = []
    for step in graded:
        if step["continue_on_error"]:
            problems.append(f"{step['id']}: continue-on-error swallows the step's failure")
        for line in step["masked"]:
            problems.append(f"{step['id']}: MASKED — {line['why']} :: {line['line']}")
        for line in step["unknown"]:
            # FAIL CLOSED. For a graded security step, "cannot prove it propagates" is a finding.
            problems.append(f"{step['id']}: UNKNOWN — {line['why']} :: {line['line']}")

    # GATE 4N-I27R. Non-graded steps do not fail the job — they are setup, diagnostics and
    # build bands whose outcome no guard reads — but they are REPORTED rather than skipped, so
    # a live mask outside the graded set is visible instead of invisible. `ci.yml`'s contract
    # drift step legitimately carries `|| true`; that is now stated, not hidden.
    non_graded_observations = [
        {"step": s["id"], "job": s["job"], "has_id": s["has_id"],
         "masked": [l["line"] for l in s["masked"]],
         "unknown": [l["line"] for l in s["unknown"]]}
        for s in non_graded if s["masked"] or s["unknown"]]

    analysed = sum(len(s["lines"]) for s in result["steps"])
    return {
        "graded_steps": len(graded),
        "workflow_steps_total": len(result["steps"]),
        "non_graded_steps": len(non_graded),
        "steps_without_an_id": sum(1 for s in result["steps"] if not s["has_id"]),
        "command_lines_analysed": analysed,
        "shell": result["shell"],
        "masked_lines": sum(len(s["masked"]) for s in graded),
        "unknown_lines": sum(len(s["unknown"]) for s in graded),
        "non_graded_observations": non_graded_observations,
        "steps": result["steps"],
        "problems": problems,
        "method": "propagation must be PROVEN; an unrecognised construct is UNKNOWN and fails "
                  "closed. Recognising bad forms would let the unlisted form pass.",
        "clean": not problems,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = check()
    except PropagationError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("FAILURE PROPAGATION: refused")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  {result['graded_steps']} graded of {result['workflow_steps_total']} workflow "
              f"steps ({result['steps_without_an_id']} without an id); "
              f"{result['command_lines_analysed']} command lines; "
              f"masked {result['masked_lines']}; unknown {result['unknown_lines']}")
        for obs in result["non_graded_observations"]:
            print(f"    non-graded {obs['step']}: masked={obs['masked']} "
                  f"unknown={obs['unknown']}")
        for problem in result["problems"]:
            print(f"    {problem}", file=sys.stderr)
        print("FAILURE PROPAGATION:", "clean" if result["clean"] else "MASKED")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
