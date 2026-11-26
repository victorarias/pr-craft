#!/usr/bin/env python3
"""Lint pull request bodies before `gh pr create` or `gh pr edit` runs.

Hook mode (no arguments): read a Claude Code PreToolUse event on stdin. When a
Bash command creates or edits a PR with a body that breaks the rules, print a
`deny` decision with the exact problems and a short rubric. Anything else is
allowed. A command this script cannot read is allowed with a note on stderr.

CLI mode: `pr_body_lint.py --check-file pr.md [--diff-files N] [--diff-lines N]
[--json]` exits 0 when the body passes and 2 with the problems when it fails.

Set PR_CRAFT_SKIP=1 to skip every check.

Standard library only, so it runs under `python3 -I` with nothing installed.
"""
import json
import os
import sys

SKILL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "skills", "pr-craft", "SKILL.md")


# ---------------------------------------------------------------------------
# Rules


def _rules():
    import re

    words = lambda *ws: re.compile(r"\b(?:" + "|".join(ws) + r")\b", re.IGNORECASE)
    return {
        "re": re,
        "heading": re.compile(
            r"^\s{0,3}(?:#{1,6}\s+|\*\*|__)\W*"
            r"(testing|tests|verification|validation|test plan|how to test)\b",
            re.IGNORECASE),
        # Headings that tell a reviewer or deployer to act; allowed on small diffs.
        "action_heading": re.compile(r"(manual verification|before merging|rollout)\b",
                                     re.IGNORECASE),
        # Phrase rules: (rule_id, pattern, fix)
        "phrases": [
            ("ai-vocab", words(
                "additionally", "crucial", "delve[sd]?", "delving", "enduring",
                "enhance[sd]?", "enhancements?", "enhancing", "foster(?:s|ed|ing)?",
                "garner(?:s|ed|ing)?", "interplay", "intricate", "landscape",
                "pivotal", "showcase[sd]?", "showcasing", "tapestry", "testament",
                "underscore[sd]?", "underscoring", "vibrant"),
             "replace with a plain word"),
            ("fancy-is", words("serves as", "stands as", "boasts?"),
             'say "is" or "has"'),
            ("not-just", re.compile(r"\bnot (?:just|only|merely)\b[^.\n]{0,80}?\bbut\b",
                                    re.IGNORECASE),
             "state the point directly"),
            ("chatbot", words(
                "i hope this helps", "let me know if", "of course!?", "certainly!",
                "smoking gun", "great question", "you're absolutely right",
                "happy to help", "feel free to"),
             "delete it"),
            ("filler", words(
                "in order to", "due to the fact that", "it is important to note",
                "it's important to note", "it is worth noting", "it's worth noting"),
             'cut it ("in order to" becomes "to")'),
            ("plain-word", words(
                "utilize[sd]?", "utilizing", "leverage[sd]?", "leveraging",
                "facilitate[sd]?", "facilitating", "numerous", "in the event that"),
             'use the plain word ("use", "help", "many", "if")'),
            ("breadcrumb", words(
                "as discussed", "as requested", "as you asked", "per our discussion",
                "per the conversation", "per our conversation", "in this session",
                "the user asked", "as mentioned earlier"),
             "write for a reviewer who never saw the conversation"),
        ],
    }


# Prose word budget by diff size. Fenced blocks (code, Mermaid) do not count.
# (bucket, max changed lines, max prose words). Every accepted rewrite in the
# eval fixtures (tests/pr-description) fits with room: the longest is 155 words,
# inline code included, for a 1,339-line change.
BUDGETS = (("tiny", 30, 120), ("small", 200, 160), ("medium", 800, 220),
           ("large", None, 270))
UNKNOWN_BUDGET = 270
# Words under '## Before merging' or '## Rollout' that don't count toward the budget.
ACTION_SECTION_WORDS = 60
# code-heavy-list: this many bullets outside merge, rollout and verification
# sections, each with this many inline code spans of 4 or more characters.
CODE_HEAVY_BULLETS = 3
CODE_HEAVY_SPANS = 2

MERMAID_TYPES = (
    "flowchart", "graph", "sequenceDiagram", "stateDiagram", "stateDiagram-v2",
    "classDiagram", "erDiagram", "gantt", "pie", "journey", "gitGraph", "mindmap",
    "timeline", "quadrantChart", "requirementDiagram", "C4Context", "C4Container",
    "C4Component", "C4Dynamic", "C4Deployment", "block-beta", "sankey-beta",
    "xychart-beta", "packet-beta", "architecture-beta", "kanban", "radar-beta")


def diff_bucket(lines):
    if lines is None:
        return "unknown", UNKNOWN_BUDGET
    for name, limit, budget in BUDGETS:
        if limit is None or lines <= limit:
            return name, budget
    raise AssertionError("unreachable")


def split_fences(body):
    """Return (prose_lines, fenced_blocks). Each block is (info, text)."""
    prose, blocks, fence, info, buf = [], [], None, "", []
    for line in body.splitlines():
        stripped = line.lstrip()
        if fence is None and (stripped.startswith("```") or stripped.startswith("~~~")):
            fence = stripped[:3]
            info = stripped[3:].strip().lower()
            buf = []
        elif fence is not None and stripped.startswith(fence):
            blocks.append((info, "\n".join(buf)))
            fence = None
        elif fence is not None:
            buf.append(line)
        else:
            prose.append(line)
    if fence is not None:  # unclosed fence: treat its lines as a block
        blocks.append((info, "\n".join(buf)))
    return prose, blocks


def lint(body, diff_lines=None, env=None):
    """Return a dict: decision, violations, prose_words, budget, diff_bucket."""
    r = _rules()
    re = r["re"]
    env = os.environ if env is None else env
    prose_lines, blocks = split_fences(body)
    violations = []

    def add(rule_id, message, excerpt):
        violations.append({"rule_id": rule_id, "message": message, "excerpt": excerpt[:80]})

    for line in prose_lines:
        if r["heading"].match(line):
            add("banned-heading",
                "Remove this section. Reviewers see automated checks in CI. "
                "Keep evidence beyond CI under '## Manual verification'.", line.strip())

    # Inline code spans are quotes of code, so they are exempt from style rules.
    prose = re.sub(r"`[^`\n]*`", "", "\n".join(prose_lines))
    for ch, name, fix in (("—", "em-dash", "use a period or a comma"),
                          ("–", "en-dash", "use a period, a comma or a hyphen")):
        for line in prose.splitlines():
            if ch in line:
                add(name, f"Remove the {name.replace('-', ' ')}: {fix}.", line.strip())
    for line in prose.splitlines():
        if any(c in line for c in "“”‘’"):
            add("curly-quote", "Use straight quotes.", line.strip())
    for rule_id, pattern, fix in r["phrases"]:
        for m in pattern.finditer(prose):
            add(rule_id, f'"{m.group(0)}": {fix}.', _context(prose, m.start(), m.end()))

    # Inline code still takes the reader's time, so it counts toward the budget.
    text = "\n".join(prose_lines).replace("`", "")
    text = re.sub(r"\]\([^)]*\)", "]", text)  # link targets are not prose
    text = re.sub(r"https?://\S+", "", text)
    count = lambda t: len(re.findall(r"[A-Za-z0-9][\w'’./-]*", t))
    # A short merge or rollout section is exempt, so the budget never squeezes out
    # the steps a reviewer or deployer must follow.
    action_words, in_action = 0, False
    for line in text.splitlines():
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*)", line)
        if m:
            in_action = bool(r["action_heading"].match(m.group(1))) and \
                not m.group(1).lower().startswith("manual verification")
        elif in_action:
            action_words += count(line)
    prose_words = count(text) - min(action_words, ACTION_SECTION_WORDS)
    bucket, budget = diff_bucket(diff_lines)
    if prose_words > budget:
        add("length-budget",
            f"Prose is {prose_words} words; the budget for a {bucket} diff is {budget}. "
            "Cut what the reviewer can read in the diff. Inline code counts; code and Mermaid "
            "blocks do not, "
            f"nor the first {ACTION_SECTION_WORDS} words under '## Before merging' or '## Rollout', "
            "so keep merge and rollout steps there instead of cutting them.",
            "")

    # A bullet list whose items each quote several identifiers is an inventory of
    # the diff. Merge, rollout and verification steps may name what to run.
    heavy, in_action = [], False
    for line in prose_lines:
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*)", line)
        if m:
            in_action = bool(r["action_heading"].match(m.group(1)))
        elif not in_action and re.match(r"^\s*[-*]\s+", line) and \
                len(re.findall(r"`[^`\n]{4,}`", line)) >= CODE_HEAVY_SPANS:
            heavy.append(line.strip())
    if len(heavy) >= CODE_HEAVY_BULLETS:
        add("code-heavy-list",
            f"{len(heavy)} bullets each quote identifiers the reviewer can read in the Files tab. "
            "Summarize what changed in one to three plain sentences and keep only the risks or "
            "caveats to check.", heavy[0])

    if bucket in ("tiny", "small"):
        for line in prose_lines:
            m = re.match(r"^\s{0,3}#{1,6}\s+(.*)", line)
            if m and not r["action_heading"].match(m.group(1)):
                add("small-diff-heading",
                    f"A diff this size ({diff_lines} changed lines) reads as a few paragraphs. "
                    "Drop the heading and keep the text. Only '## Manual verification', "
                    "'## Before merging' and '## Rollout' may stay.", line.strip())

    for info, block in blocks:
        if info.split()[:1] == ["mermaid"]:
            problem = mermaid_problem(block, env)
            if problem:
                add("mermaid-syntax", f"Fix the Mermaid block: {problem}", block.strip()[:80])

    return {"decision": "deny" if violations else "allow", "violations": violations,
            "prose_words": prose_words, "budget": budget, "diff_bucket": bucket}


def _context(text, start, end):
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    return text[line_start: line_end if line_end != -1 else len(text)].strip()


def mermaid_problem(block, env):
    lines = [l for l in block.splitlines() if l.strip() and not l.strip().startswith("%%")]
    if not lines:
        return "the block is empty."
    first = lines[0].strip().split()[0]
    if first not in MERMAID_TYPES:
        return f"'{first}' is not a Mermaid diagram type; start with e.g. 'flowchart LR' or 'sequenceDiagram'."
    for line in lines:
        stripped = _strip_quoted(line)
        for open_, close in ("[]", "()", "{}"):
            if stripped.count(open_) != stripped.count(close):
                return f"unbalanced '{open_}{close}' in: {line.strip()[:60]}"
        if line.count('"') % 2:
            return f"unbalanced quote in: {line.strip()[:60]}"
    return _mmdc_problem(block, env)


def _strip_quoted(line):
    out, quoted = [], False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        elif not quoted:
            out.append(ch)
    return "".join(out)


def _mmdc_problem(block, env):
    """Parse with mermaid-cli when it is installed. Only a parser error counts;
    a broken mmdc install or a timeout is ignored."""
    import shutil
    import subprocess
    import tempfile

    mmdc = shutil.which("mmdc", path=env.get("PATH"))
    if not mmdc:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "d.mmd")
        with open(src, "w") as f:
            f.write(block)
        try:
            proc = subprocess.run([mmdc, "-q", "-i", src, "-o", os.path.join(tmp, "d.svg")],
                                  capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return None
    if proc.returncode == 0:
        return None
    out = proc.stderr + proc.stdout
    for marker in ("Parse error", "Lexical error", "Syntax error"):
        i = out.find(marker)
        if i != -1:
            return " ".join(out[i:].splitlines()[:2])[:160]
    return None


RUBRIC = """PR body rules:
- Open with the problem in the reader's terms: what breaks, costs or blocks today, then what changes. Not with a later PR that will use this one. For a follow-up PR in a stack, stating what changes and linking to the background is enough.
- Do not restate or inventory the diff: no file-by-file walk, no change list of resources, roles, fields or values, no step-by-step walkthrough of internal mechanics, no test lists, and no spelled-out conditions, regexes or log formats. State what changed in one to three plain sentences and keep only what a reviewer must judge or act on.
- Say plainly what a reviewer or deployer must act on: what must happen before merging ("Merge only after ..."; checks still to do are not results), what stays unused until which PR. Say what each other PR in a stack actually does.
- Claim no more than the code does, in prose and diagrams. Before you submit, check each "all", "every", "only" and "never", each diagram arrow and each claim about another PR against the code or the context, and cut or narrow what you can't point to.
- If the PR adds or changes a call path (a cache, a retry, a new step), draw it as a Mermaid sequenceDiagram. Add a schema example table only when this PR creates the table or manifest. A diagram or table replaces prose; never repeat the same mapping in both. If this PR sits between other PRs in a stack, show it as a flowchart LR; if it only has to land before another PR, say that in a sentence.
- Diffs of 200 lines or fewer get no headings except '## Manual verification', '## Before merging' or '## Rollout'. Never a Testing, Verification, Validation or Test plan section; CI shows those checks.
- Plain words, straight quotes, no em dashes. Write for a reviewer who never saw your session.
- Stay within the prose word budget for the diff size. Inline code counts; code and Mermaid blocks do not, nor a short '## Before merging' or '## Rollout' section."""


def format_reason(result, source=""):
    lines = [f"pr-craft: the PR body{(' from ' + source) if source else ''} breaks "
             f"{len(result['violations'])} rule(s). None of this command ran, so any file it writes "
             "still holds its old content or does not exist. Fix the body and rerun the whole "
             "command."]
    for v in result["violations"]:
        excerpt = f"  [{v['excerpt']}]" if v["excerpt"] else ""
        lines.append(f"- {v['rule_id']}: {v['message']}{excerpt}")
    lines.append("")
    lines.append(RUBRIC)
    if os.path.exists(SKILL_PATH):
        lines.append(f"Full guide: {SKILL_PATH}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shell parsing


class Unreadable(Exception):
    """The command runs gh pr create/edit, but the body cannot be recovered."""


class NotWrittenYet(Exception):
    """The body file is produced by another step of the same command, so it can't be checked."""


NOT_WRITTEN_REASON = (
    "pr-craft: the body file {path} does not exist yet; another step of this command produces "
    "it, so the body can't be checked. Write the body file in its own command first, then run "
    "gh pr {action} with --body-file in a separate command.")
VARIABLE_PATH_REASON = (
    "pr-craft: the body file {path} depends on a shell variable, so the body can't be checked. "
    "Write the body to a literal path in one command, then run gh pr {action} --body-file "
    "<that path> in a separate command.")


def _extract_heredocs(command):
    """Replace each heredoc with a marker token and return (text, {marker: body})."""
    import re

    op = re.compile(r"(?<!<)<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][\w]*))")
    lines = command.split("\n")
    out, bodies, i = [], {}, 0
    while i < len(lines):
        line = lines[i]
        pending = []

        def repl(m):
            marker = f"__PRCRAFT_HD{len(bodies) + len(pending)}__"
            pending.append((marker, m.group(2) or m.group(3) or m.group(4), m.group(1) == "-"))
            return " " + marker + " "

        out.append(op.sub(repl, line))
        i += 1
        for marker, delim, strip_tabs in pending:
            body = []
            while i < len(lines):
                candidate = lines[i].lstrip("\t") if strip_tabs else lines[i]
                i += 1
                if candidate == delim:
                    break
                body.append(candidate)
            else:
                raise Unreadable(f"heredoc '{delim}' is not closed")
            bodies[marker] = "\n".join(body) + "\n"
    text = "\n".join(out)
    # "$(cat <<'EOF' ... EOF\n)" is the heredoc's text in place.
    text = re.sub(r"\$\(\s*cat\s+(__PRCRAFT_HD\d+__)\s*\)", r"\1", text)
    return text, bodies


def _tokenize(text):
    import shlex

    text = text.replace("\\\n", "")
    lx = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
    lx.whitespace = " \t\r"
    lx.whitespace_split = True
    try:
        return list(lx)
    except ValueError as e:
        raise Unreadable(f"shell parse error: {e}")


SEPARATORS = {"&&", "||", ";", "&", "\n", "(", ")", ";;"}


def _simple_commands(tokens):
    """Yield lists of simple commands, grouped by pipeline."""
    pipeline, cmd = [], []
    for tok in tokens:
        if tok in SEPARATORS:
            if cmd:
                pipeline.append(cmd)
            if pipeline:
                yield pipeline
            pipeline, cmd = [], []
        elif tok in ("|", "|&"):
            pipeline.append(cmd)
            cmd = []
        else:
            cmd.append(tok)
    if cmd:
        pipeline.append(cmd)
    if pipeline:
        yield pipeline


def _split_assignments(cmd):
    import re

    assigns = {}
    while cmd and re.match(r"^[A-Za-z_]\w*=", cmd[0]):
        k, v = cmd[0].split("=", 1)
        assigns[k] = v
        cmd = cmd[1:]
    return assigns, cmd


def _gh_pr_action(cmd):
    """Return 'create', 'edit' or None, and the argument list after it."""
    if not cmd or os.path.basename(cmd[0]) != "gh":
        return None, []
    args, i = cmd[1:], 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-R", "--repo") else 1
    if i + 1 < len(args) and args[i] == "pr" and args[i + 1] in ("create", "edit"):
        return args[i + 1], args[i + 2:]
    return None, []


def _flag_value(args, names):
    """Return the last value of the given flags, supporting --flag=value."""
    value = None
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                value = args[i + 1]
            elif a.startswith(n + "=") and n.startswith("--"):
                value = a[len(n) + 1:]
    return value


def _subst(value, bodies):
    for marker, body in bodies.items():
        value = value.replace(marker, body.rstrip("\n"))
    return value


def _redirect_target(cmd):
    for i, tok in enumerate(cmd):
        if tok in (">", ">>", ">|") and i + 1 < len(cmd):
            return cmd[i + 1]
    return None


def _stdin_heredoc(cmd, bodies):
    for tok in cmd:
        if tok in bodies:
            return bodies[tok]
    return None


def find_pr_bodies(command, cwd, env):
    """Return a list of dicts for each gh pr create/edit in the command:
    {action, body (str or None), source, base, cwd, skip}."""
    if "gh" not in command or "pr" not in command:
        return []
    text, bodies = _extract_heredocs(command)
    written = {}  # path as written in the command -> content
    found = []
    for pipeline in _simple_commands(_tokenize(text)):
        stdout_of_prev = None
        for cmd in pipeline:
            assigns, cmd = _split_assignments(cmd)
            if not cmd:
                continue
            name = os.path.basename(cmd[0])
            heredoc = _stdin_heredoc(cmd, bodies)
            if name == "cd" and len(cmd) > 1:
                target = os.path.expanduser(cmd[1].replace("${PWD}", cwd).replace("$PWD", cwd))
                target = os.path.join(cwd, target)
                if "$" not in target and os.path.isdir(target):
                    cwd = target
            if name in ("cat", "tee"):
                target = _redirect_target(cmd) if name == "cat" else next(
                    (t for t in cmd[1:] if not t.startswith("-") and t not in bodies
                     and t not in (">", ">>")), None)
                if heredoc is not None and target:
                    written[target] = heredoc
                stdout_of_prev = heredoc if (name == "tee" or not _redirect_target(cmd)) else None
                continue
            action, args = _gh_pr_action(cmd)
            if action:
                found.append(_resolve(action, args, assigns, heredoc, stdout_of_prev,
                                      written, bodies, cwd, env))
            stdout_of_prev = None
    return found


def _resolve(action, args, assigns, heredoc, piped, written, bodies, cwd, env):
    entry = {"action": action, "body": None, "source": "", "cwd": cwd,
             "base": _flag_value(args, ["-B", "--base"]),
             "skip": assigns.get("PR_CRAFT_SKIP") == "1"}
    inline = _flag_value(args, ["-b", "--body"])
    path = _flag_value(args, ["-F", "--body-file"])
    if inline is not None:
        if "$(" in inline:
            raise Unreadable("the --body text comes from a command substitution")
        entry["body"], entry["source"] = _subst(inline, bodies), "--body"
    elif path == "-":
        stdin = heredoc if heredoc is not None else piped
        if stdin is None:
            raise Unreadable("--body-file - reads stdin from a source this hook cannot see")
        entry["body"], entry["source"] = stdin, "stdin"
    elif path is not None:
        if path in written:
            entry["body"] = written[path]
            entry["written_here"] = os.path.join(cwd, os.path.expanduser(path))
        else:
            full = os.path.join(cwd, os.path.expanduser(os.path.expandvars(path)))
            if "$" in full:
                raise NotWrittenYet(VARIABLE_PATH_REASON.format(path=path, action=action))
            if not os.path.isfile(full):
                raise NotWrittenYet(NOT_WRITTEN_REASON.format(path=path, action=action))
            with open(full, encoding="utf-8", errors="replace") as f:
                entry["body"] = f.read()
        entry["source"] = path
    return entry


# ---------------------------------------------------------------------------
# Diff size


def changed_lines(cwd, base):
    """Lines added plus removed between the PR base and HEAD, or None."""
    import re
    import subprocess

    def git(*a):
        try:
            p = subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return p.stdout.strip() if p.returncode == 0 else None

    candidates = []
    if base:
        candidates += [f"origin/{base}", base]
    head = git("rev-parse", "--abbrev-ref", "origin/HEAD")
    if head:
        candidates.append(head)
    candidates += ["origin/main", "origin/master"]
    for ref in candidates:
        stat = git("diff", "--shortstat", f"{ref}...HEAD")
        if stat is not None:
            nums = [int(n) for n in re.findall(r"(\d+) (?:insertion|deletion)", stat)]
            return sum(nums)
    return None


# ---------------------------------------------------------------------------
# Entry points


def _unwritten_note(entry):
    """Agents tend to patch the body file after a denial, as if the denied write had run."""
    path = entry.get("written_here")
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            first = next((line.strip() for line in f if line.strip()), "")
    except OSError:
        return f"\n\n{entry['source']} was not written. Write the whole body again."
    return (f"\n\n{entry['source']} was not written and still holds an older body that starts "
            f"\"{first[:80]}\". Write the whole body again; do not edit that file.")


def hook_main(stdin, env):
    raw = stdin.read()
    if env.get("PR_CRAFT_SKIP") == "1" or "gh" not in raw:
        return 0
    try:
        event = json.loads(raw)
        if event.get("tool_name", "Bash") != "Bash":
            return 0
        command = (event.get("tool_input") or {}).get("command") or ""
        cwd = event.get("cwd") or os.getcwd()
        reasons = []
        try:
            entries = find_pr_bodies(command, cwd, env)
        except NotWrittenYet as e:
            if "PR_CRAFT_SKIP=1" in command:
                return 0
            entries, reasons = [], [str(e)]
        for entry in entries:
            if entry["skip"] or entry["body"] is None:
                continue
            result = lint(entry["body"], changed_lines(entry["cwd"], entry["base"]), env)
            if result["decision"] == "deny":
                reasons.append(format_reason(result, entry["source"]) + _unwritten_note(entry))
    except Unreadable as e:
        print(f"pr-craft: allowing a command it could not read ({e}).", file=sys.stderr)
        return 0
    except Exception as e:  # never block gh because the linter broke
        print(f"pr-craft: linter error, allowing ({type(e).__name__}: {e}).", file=sys.stderr)
        return 0
    if reasons:
        json.dump({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "\n\n".join(reasons),
        }}, sys.stdout)
    return 0


def cli_main(argv, env):
    import argparse

    p = argparse.ArgumentParser(description="Lint a pull request body.")
    p.add_argument("--check-file", required=True, help="PR body file, or - for stdin")
    p.add_argument("--diff-files", type=int, help="files changed (informational)")
    p.add_argument("--diff-lines", type=int, help="lines added plus removed")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    a = p.parse_args(argv)
    if env.get("PR_CRAFT_SKIP") == "1":
        return 0
    if a.check_file == "-":
        body = sys.stdin.read()
    else:
        with open(a.check_file, encoding="utf-8") as f:
            body = f.read()
    result = lint(body, a.diff_lines, env)
    if a.json:
        json.dump(result, sys.stdout, indent=2)
        print()
    elif result["decision"] == "deny":
        print(format_reason(result, a.check_file))
    return 2 if result["decision"] == "deny" else 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli_main(sys.argv[1:], os.environ))
    sys.exit(hook_main(sys.stdin, os.environ))
