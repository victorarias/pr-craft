"""Drive plugin/scripts/pr_body_lint.py as Claude Code and a user do:
a PreToolUse event on stdin, or the --check-file CLI. Every run uses python3 -I with
a PATH that has git but no mmdc, so the results do not depend on the machine."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugin/scripts/pr_body_lint.py"
PLAIN_PATH = "/usr/bin:/bin"

LEAN = textwrap.dedent("""\
    Uploads that fail with a 503 now retry up to three times with backoff instead
    of failing the whole batch. Last week one flaky storage node failed 40 batches.

    ```mermaid
    sequenceDiagram
      participant W as Worker
      participant S as Storage
      W->>S: PUT chunk
      S-->>W: 503
      W->>S: PUT chunk (retry)
    ```

    Retries only cover 503 and connection resets. A 4xx still fails at once.

    ## Manual verification

    Ran 500 uploads against a staging bucket with 10% injected faults. All landed.
    """)

# Shaped like a typical bloated first draft: a summary, a bullet per file and a
# testing section.
BLOATED = "## Summary\n\n" + ("This change updates the ingestion worker so that each "
    "upload request is retried when the storage service returns an error, and it "
    "records the attempt count on the batch record for later inspection. ") * 12 + \
    "\n\n## Changes\n\n" + "".join(
        f"- `src/module_{i}.py`: adjusts the handler to pass the retry policy through "
        "to the client and updates the related type hints.\n" for i in range(20)) + \
    "\n## Testing\n\n- Unit tests pass\n- Linters pass\n"


def run_hook(command, cwd=None, env_extra=None):
    env = {"PATH": PLAIN_PATH, **(env_extra or {})}
    event = {"session_id": "t", "hook_event_name": "PreToolUse", "tool_name": "Bash",
             "tool_input": {"command": command}, "cwd": str(cwd or tempfile.gettempdir())}
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT)], input=json.dumps(event),
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    decision = json.loads(proc.stdout)["hookSpecificOutput"] if proc.stdout.strip() else None
    return decision, proc.stderr


def run_cli(body, *args, env_extra=None):
    env = {"PATH": PLAIN_PATH, **(env_extra or {})}
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT), "--check-file", "-", "--json", *args],
                          input=body, capture_output=True, text=True, env=env, timeout=30)
    return proc.returncode, (json.loads(proc.stdout) if proc.stdout.strip() else None)


def rule_ids(result):
    return {v["rule_id"] for v in result["violations"]}


class ShellExtraction(unittest.TestCase):
    """Each way agents pass a body to gh reaches the linter (deny), and the same
    command with a clean body is allowed."""

    BAD = "Adds retries.\n\n## Testing\n\nRan the tests.\n"
    GOOD = "Adds retries for 503 responses so one flaky node no longer fails a batch.\n"

    def assert_gate(self, make_command, cwd=None):
        denied, _ = run_hook(make_command(self.BAD), cwd)
        self.assertIsNotNone(denied, "bad body was allowed")
        self.assertEqual(denied["permissionDecision"], "deny")
        self.assertIn("banned-heading", denied["permissionDecisionReason"])
        allowed, _ = run_hook(make_command(self.GOOD), cwd)
        self.assertIsNone(allowed, "good body was denied")

    def test_long_body_flag(self):
        self.assert_gate(lambda b: f"gh pr create --title t --body '{b}'")

    def test_long_body_flag_with_equals(self):
        self.assert_gate(lambda b: f"gh pr create --title t --body='{b}'")

    def test_short_body_flag(self):
        self.assert_gate(lambda b: f'gh pr edit 12 -b "{b}"')

    def test_repo_flag_before_subcommand(self):
        self.assert_gate(lambda b: f"gh -R owner/repo pr create -b '{b}'")

    def test_body_file_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            def cmd(body):
                Path(tmp, "pr.md").write_text(body)
                return "gh pr create --title t --body-file pr.md"
            self.assert_gate(cmd, cwd=tmp)

    def test_short_body_file_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            def cmd(body):
                Path(tmp, "body.md").write_text(body)
                return f"gh pr edit -F {tmp}/body.md"
            self.assert_gate(cmd)

    def test_body_file_stdin_heredoc(self):
        self.assert_gate(lambda b: f"gh pr create --title t -F - <<'EOF'\n{b}EOF")

    def test_heredoc_piped_to_body_file_stdin(self):
        self.assert_gate(lambda b: f"cat <<'EOF' | gh pr create --title t -F -\n{b}EOF")

    def test_heredoc_written_then_read_in_same_command(self):
        # The file does not exist yet when the hook runs.
        self.assert_gate(lambda b: f"cat > $S/pr.md <<'EOF'\n{b}EOF\ngh pr create --body-file $S/pr.md")
        self.assert_gate(lambda b: f"cat > $S/pr.md <<'EOF' && gh pr create --body-file $S/pr.md\n{b}EOF")

    def test_heredoc_with_redirect_after_operator(self):
        self.assert_gate(lambda b: f"cat <<EOF > pr.md\n{b}EOF\ngh pr edit 3 -F pr.md")

    def test_command_substitution_heredoc_in_body(self):
        self.assert_gate(lambda b: f'gh pr create --title "t" --body "$(cat <<\'EOF\'\n{b}EOF\n)"')

    def test_tee_heredoc(self):
        self.assert_gate(lambda b: f"tee /tmp/x/pr.md <<'EOF' >/dev/null\n{b}EOF\ngh pr create -F /tmp/x/pr.md")

    def test_cd_changes_where_body_file_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "sub").mkdir()
            def cmd(body):
                Path(tmp, "sub", "pr.md").write_text(body)
                return "cd sub && gh pr create -F pr.md"
            self.assert_gate(cmd, cwd=tmp)

    def test_second_gh_command_in_chain(self):
        self.assert_gate(lambda b: f"git push -u origin HEAD && gh pr create --body '{b}'")


class OtherCommandsAllowed(unittest.TestCase):
    def test_non_pr_commands(self):
        for cmd in ("ls -la", "gh pr view 12", "gh pr list", "git commit -m '## Testing'",
                    "gh issue create --body '## Testing—x'", "echo gh pr create"):
            with self.subTest(cmd=cmd):
                self.assertEqual(run_hook(cmd), (None, ""))

    def test_pr_without_body(self):
        self.assertEqual(run_hook("gh pr create --fill")[0], None)
        self.assertEqual(run_hook("gh pr edit 3 --title 'New title'")[0], None)

    def test_non_bash_tool(self):
        event = {"tool_name": "Write", "tool_input": {"content": "gh pr create -b '## Testing'"}}
        proc = subprocess.run([sys.executable, "-I", str(SCRIPT)], input=json.dumps(event),
                              capture_output=True, text=True, env={"PATH": PLAIN_PATH})
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))


class Rules(unittest.TestCase):
    def assert_flags(self, body, rule):
        code, result = run_cli(body)
        self.assertEqual(code, 2, f"{rule} not flagged in {body!r}")
        self.assertIn(rule, rule_ids(result))

    def test_banned_headings(self):
        for heading in ("## Testing", "### Verification", "# Validation", "## Test Plan",
                        "## Test plan", "**Testing**", "## Tests", "## How to test"):
            with self.subTest(heading=heading):
                self.assert_flags(f"Adds retries.\n\n{heading}\n\nRan it.\n", "banned-heading")

    def test_manual_verification_heading_allowed(self):
        self.assertEqual(run_cli("Adds retries.\n\n## Manual verification\n\nRan it on staging.\n")[0], 0)

    def test_typography(self):
        self.assert_flags("Adds retries — for 503s.\n", "em-dash")
        self.assert_flags("Adds retries – for 503s.\n", "en-dash")
        self.assert_flags("Adds “retries”.\n", "curly-quote")
        self.assert_flags("Adds the worker’s retries.\n", "curly-quote")

    def test_phrases(self):
        cases = [
            ("This is a pivotal fix.", "ai-vocab"),
            ("Additionally, it logs.", "ai-vocab"),
            ("We delve into retries.", "ai-vocab"),
            ("The module serves as a gateway.", "fancy-is"),
            ("It is not just faster, but safer.", "not-just"),
            ("Let me know if you have questions.", "chatbot"),
            ("I hope this helps!", "chatbot"),
            ("Retries exist in order to survive blips.", "filler"),
            ("We leverage the client pool.", "plain-word"),
            ("We utilize the client pool.", "plain-word"),
            ("As discussed, this adds retries.", "breadcrumb"),
        ]
        for sentence, rule in cases:
            with self.subTest(sentence=sentence):
                self.assert_flags(sentence + "\n", rule)

    def test_code_is_exempt(self):
        body = ("Renames the `leverage` flag.\n\n```text\n## Testing — “pivotal”\n```\n")
        code, result = run_cli(body)
        self.assertEqual((code, result["violations"]), (0, []))

    def test_word_budget_scales_with_diff(self):
        body = " ".join(["word"] * 150) + "\n"
        self.assertEqual(run_cli(body, "--diff-lines", "20")[0], 2)
        self.assertEqual(run_cli(body, "--diff-lines", "150")[0], 0)
        self.assertEqual(run_cli(body)[0], 0)  # unknown diff uses the largest budget

    def test_inline_code_counts_toward_budget_but_not_style_rules(self):
        body = " ".join(["`ident`"] * 125) + " Renames the `leverage` flag.\n"
        code, result = run_cli(body, "--diff-lines", "10")
        self.assertEqual(rule_ids(result), {"length-budget"}, result)
        self.assertEqual(result["prose_words"], 129)

    def test_large_diff_budget(self):
        body = " ".join(["word"] * 260) + "\n"
        self.assertEqual(run_cli(body, "--diff-lines", "900")[0], 0)
        self.assertEqual(run_cli(body, "--diff-lines", "500")[0], 2)  # medium is 220
        self.assertEqual(run_cli(" ".join(["word"] * 280) + "\n")[0], 2)  # unknown is 270

    def test_code_heavy_list_denied(self):
        inventory = "".join(f"- Adds `subscription_{i}` with `roles/viewer_{i}`.\n" for i in range(3))
        code, result = run_cli("Records the missing queue.\n\n" + inventory, "--diff-lines", "300")
        self.assertEqual(code, 2)
        self.assertIn("code-heavy-list", rule_ids(result))

    def test_code_heavy_list_allows_short_lists_action_steps_and_short_keys(self):
        two = "".join(f"- Adds `subscription_{i}` with `roles/viewer_{i}`.\n" for i in range(2))
        keys = "".join(f"- `J` and `N` move to item {i}.\n" for i in range(4))
        steps = "## Rollout\n\n" + "".join(f"- Run `deploy_{i}` then `check_{i}`.\n" for i in range(4))
        for body in (two, keys, "Why.\n\n" + steps):
            code, result = run_cli("Records the missing queue.\n\n" + body, "--diff-lines", "300")
            self.assertNotIn("code-heavy-list", rule_ids(result), body)

    def test_small_diffs_keep_only_action_headings(self):
        body = "Why.\n\n## Stack\n\n#1 -> #2\n\n## Before merging\n\nRun it.\n"
        code, result = run_cli(body, "--diff-lines", "70")
        self.assertEqual(code, 2)
        self.assertEqual([v["excerpt"] for v in result["violations"]], ["## Stack"])
        self.assertEqual(run_cli(body, "--diff-lines", "201")[0], 0)
        self.assertEqual(run_cli(LEAN, "--diff-lines", "70")[0], 0)

    def test_short_merge_section_does_not_count_toward_budget(self):
        steps = "## Before merging\n\n" + " ".join(["step"] * 50) + "\n"
        prose = " ".join(["word"] * 110) + "\n\n"
        code, result = run_cli(prose + steps, "--diff-lines", "10")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["prose_words"], 112)  # the two heading words still count
        long_steps = "## Rollout\n\n" + " ".join(["step"] * 80) + "\n"
        self.assertEqual(run_cli(prose + long_steps, "--diff-lines", "10")[0], 2)

    def test_mermaid_blocks_do_not_count_toward_budget(self):
        diagram = "```mermaid\nflowchart LR\n" + "".join(f"  A{i}[step {i}] --> A{i+1}\n" for i in range(60)) + "```\n"
        code, result = run_cli("Adds retries.\n\n" + diagram, "--diff-lines", "10")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["prose_words"], 2)

    def test_bloated_body_denied_and_lean_body_allowed(self):
        code, result = run_cli(BLOATED, "--diff-lines", "400")
        self.assertEqual(code, 2)
        self.assertTrue({"banned-heading", "length-budget"} <= rule_ids(result), result)
        self.assertGreater(result["prose_words"], 600)
        code, result = run_cli(LEAN, "--diff-lines", "400")
        self.assertEqual((code, result["violations"]), (0, []))

    def test_mermaid_syntax(self):
        bad = {
            "unknown type": "```mermaid\nflow LR\n  A --> B\n```\n",
            "unbalanced bracket": "```mermaid\nflowchart LR\n  A[start --> B\n```\n",
            "unbalanced quote": '```mermaid\nflowchart LR\n  A["start] --> B\n```\n',
            "empty": "```mermaid\n```\n",
        }
        for name, block in bad.items():
            with self.subTest(name=name):
                self.assert_flags("Adds retries.\n\n" + block, "mermaid-syntax")
        good = '```mermaid\nflowchart LR\n  A["calls (x)"] --> B{ok?}\n```\n'
        self.assertEqual(run_cli("Adds retries.\n\n" + good)[0], 0)

    @unittest.skipUnless(shutil.which("mmdc"), "mmdc not installed")
    def test_mmdc_parse_error(self):
        # Balanced brackets, but not a valid sequence diagram.
        block = "```mermaid\nsequenceDiagram\n  A->>: missing target\n```\n"
        env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")}
        self.assertEqual(run_cli("Adds retries.\n\n" + block)[0], 0)
        code, result = run_cli("Adds retries.\n\n" + block, env_extra=env)
        self.assertEqual(code, 2)
        self.assertIn("mermaid-syntax", rule_ids(result))


class HookBehavior(unittest.TestCase):
    def test_deny_reason_lists_violations_and_rubric(self):
        denied, _ = run_hook("gh pr create -b 'Adds retries — safely.\n\n## Testing\nok\n'")
        reason = denied["permissionDecisionReason"]
        self.assertIn("em-dash", reason)
        self.assertIn("banned-heading", reason)
        self.assertIn("PR body rules:", reason)
        self.assertIn("skills/pr-craft/SKILL.md", reason)
        self.assertEqual(denied["hookEventName"], "PreToolUse")

    def test_budget_uses_git_diff_against_base(self):
        body = " ".join(["word"] * 150)
        with tempfile.TemporaryDirectory() as tmp:
            git = lambda *a: subprocess.run(["git", "-C", tmp, *a], check=True, capture_output=True,
                                            env={"PATH": PLAIN_PATH, "HOME": tmp,
                                                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
            git("init", "-q", "-b", "trunk")
            Path(tmp, "a.txt").write_text("x\n")
            git("add", "."); git("commit", "-qm", "base")
            git("checkout", "-qb", "feature")
            Path(tmp, "a.txt").write_text("x\ny\n")
            git("commit", "-qam", "small change")
            denied, _ = run_hook(f"gh pr create --base trunk -b '{body}'", cwd=tmp)
            self.assertIn("tiny diff is 120", denied["permissionDecisionReason"])
            # An agent's `cd "$PWD"` must not lose the repository and fall back to the largest budget.
            denied, _ = run_hook(f'cd "$PWD" && gh pr create --base trunk -b \'{body}\'', cwd=tmp)
            self.assertIn("tiny diff is 120", denied["permissionDecisionReason"])
            Path(tmp, "a.txt").write_text("".join(f"{i}\n" for i in range(150)))
            git("commit", "-qam", "bigger change")
            self.assertIsNone(run_hook(f"gh pr create --base trunk -b '{body}'", cwd=tmp)[0])

    def test_skip_env(self):
        cmd = "gh pr create -b '## Testing\n'"
        self.assertIsNotNone(run_hook(cmd)[0])
        self.assertIsNone(run_hook(cmd, env_extra={"PR_CRAFT_SKIP": "1"})[0])
        self.assertIsNone(run_hook("PR_CRAFT_SKIP=1 " + cmd)[0])
        self.assertEqual(run_cli("## Testing\n", env_extra={"PR_CRAFT_SKIP": "1"}), (0, None))

    def test_fails_open_on_unreadable_commands(self):
        cases = {
            "unbalanced quote": "gh pr create -b 'never closed",
            "unclosed heredoc": "gh pr create -F - <<'EOF'\n## Testing\n",
            "unknown stdin": "printf '## Testing' | gh pr create -F -",
            "command substitution": 'gh pr create -b "$(cat pr.md)"',
        }
        for name, cmd in cases.items():
            with self.subTest(name=name):
                decision, stderr = run_hook(cmd)
                self.assertIsNone(decision)
                self.assertIn("pr-craft: allowing", stderr)

    def test_body_file_made_by_another_step_is_denied_with_a_way_out(self):
        cmd = ("gh pr view 3 --json body -q .body > /nonexistent/pr.md && "
               "python3 edit.py /nonexistent/pr.md && gh pr edit 3 --body-file /nonexistent/pr.md")
        denied, _ = run_hook(cmd)
        self.assertIn("does not exist yet", denied["permissionDecisionReason"])
        self.assertIn("in its own command first", denied["permissionDecisionReason"])
        self.assertIsNone(run_hook("PR_CRAFT_SKIP=1 " + cmd)[0])

    def test_body_file_behind_a_shell_variable_is_denied_with_a_way_out(self):
        cmd = 'd=$(mktemp -d) && gh pr view 3 --json body -q .body > $d/b.md && gh pr edit 3 --body-file $d/b.md'
        reason = run_hook(cmd)[0]["permissionDecisionReason"]
        self.assertIn("depends on a shell variable", reason)
        self.assertIn("literal path", reason)

    def test_denial_says_the_heredoc_write_did_not_happen(self):
        # A stale file at the same path must not look like the fixed body.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.md")
            cmd = f"cat > {path} <<'EOF'\nWhy.\n\n## Tests\n\nRan them.\nEOF\ngh pr create --body-file {path}"
            reason = run_hook(cmd)[0]["permissionDecisionReason"]
            self.assertIn("None of this command ran", reason)
            self.assertIn("was not written. Write the whole body again", reason)
            with open(path, "w") as f:
                f.write("\nTurns on the cache for another PR.\n")
            reason = run_hook(cmd)[0]["permissionDecisionReason"]
            self.assertIn('still holds an older body that starts "Turns on the cache', reason)

    def test_fails_open_on_bad_event(self):
        proc = subprocess.run([sys.executable, "-I", str(SCRIPT)], input="gh not json",
                              capture_output=True, text=True, env={"PATH": PLAIN_PATH})
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertIn("linter error, allowing", proc.stderr)

    def test_non_pr_command_cost(self):
        # The whole hook process for an unrelated command: interpreter start plus
        # the early exit. Twenty runs keep one slow start from failing the test.
        event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
        best = min(self._time([sys.executable, "-I", str(SCRIPT)], event) for _ in range(20))
        bare = min(self._time([sys.executable, "-I", "-c", "import json,sys; json.load(sys.stdin)"], event)
                   for _ in range(20))
        self.assertLess(best - bare, 0.020, f"hook {best:.3f}s vs bare interpreter {bare:.3f}s")

    @staticmethod
    def _time(argv, stdin):
        start = time.perf_counter()
        subprocess.run(argv, input=stdin, capture_output=True, text=True, env={"PATH": PLAIN_PATH})
        return time.perf_counter() - start


class Isolation(unittest.TestCase):
    def test_imports_only_the_standard_library(self):
        probe = textwrap.dedent("""\
            import runpy, sys
            before = set(sys.modules)
            g = runpy.run_path(sys.argv[1], run_name="pr_body_lint")
            g["lint"]("## Testing\\n```mermaid\\nflowchart LR\\n  A --> B\\n```\\n", 10, {"PATH": "/usr/bin:/bin"})
            g["find_pr_bodies"]("cat > f <<'EOF'\\nx\\nEOF\\ngh pr create -F f", "/tmp", {})
            g["changed_lines"]("/tmp", None)
            for name in sorted(set(sys.modules) - before):
                top = name.split(".")[0]
                if top not in sys.stdlib_module_names and not top.startswith("_"):
                    print(name)
            """)
        proc = subprocess.run([sys.executable, "-I", "-c", probe, str(SCRIPT)], capture_output=True, text=True,
                              env={"PATH": PLAIN_PATH})
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))

    def test_full_lint_runs_under_isolated_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "pr.md").write_text(BLOATED)
            proc = subprocess.run([sys.executable, "-I", "-S", str(SCRIPT), "--check-file",
                                   str(Path(tmp, "pr.md"))], capture_output=True, text=True,
                                  env={"PATH": PLAIN_PATH})
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("banned-heading", proc.stdout)


if __name__ == "__main__":
    unittest.main()
