# pr-craft

A Claude Code plugin for pull request descriptions that reviewers can read.

- A `PreToolUse` hook checks the body of every `gh pr create` and `gh pr edit`
  the agent runs. When the body breaks a rule, the hook denies the command and
  tells the agent exactly what to fix. The agent rewrites the body and retries.
- `/pr-craft` is a writing guide you invoke by hand: reviewer-first structure,
  Mermaid diagrams for flow and structure, and an example.

## Install

```text
/plugin install pr-craft --marketplace victorarias/pr-craft
```

On older Claude Code versions, add the marketplace first:

```text
/plugin marketplace add victorarias/pr-craft
/plugin install pr-craft@pr-craft
```

To try it without installing, clone the repository and start Claude Code with
`claude --plugin-dir ./plugin`.

The hook needs `python3` (3.8 or newer) and nothing else. If `mmdc`
(mermaid-cli) is on `PATH`, the hook also parses Mermaid blocks with it.

## What the hook checks

| Rule | Denies |
|---|---|
| `banned-heading` | `Testing`, `Tests`, `Verification`, `Validation`, `Test plan` or `How to test` sections. `## Manual verification` is allowed. |
| `em-dash`, `en-dash`, `curly-quote` | Typographic dashes and quotes in prose |
| `ai-vocab`, `fancy-is`, `not-just` | Words and patterns typical of generated text ("delve", "pivotal", "serves as", "not just X, but Y") |
| `chatbot`, `filler`, `plain-word` | "Let me know if", "in order to", "leverage", "utilize" |
| `breadcrumb` | References to a conversation the reviewer never saw ("as discussed") |
| `length-budget` | Prose over the word budget for the diff size |
| `code-heavy-list` | Three or more bullets outside `## Before merging`, `## Rollout` and `## Manual verification` that each quote two or more identifiers of four or more characters |
| `small-diff-heading` | Headings on a diff of 200 lines or fewer, except `## Manual verification`, `## Before merging` and `## Rollout` |
| `mermaid-syntax` | Unknown diagram types, unbalanced brackets or quotes, and `mmdc` parse errors |

Code blocks, inline code and Mermaid blocks are exempt from the style rules.
Code and Mermaid blocks are also exempt from the word budget, but inline code
counts toward it. The first 60 words under `## Before merging` or `## Rollout`
don't count either, so merge steps are never cut to fit. The budget comes from the
lines changed between the PR base and `HEAD`:

| Lines changed | Prose words |
|---|---|
| up to 30 | 120 |
| up to 200 | 160 |
| up to 800 | 220 |
| more, or unknown | 270 |

The hook reads the body from `--body`/`-b`, `--body-file`/`-F` (a file, or `-`
for stdin), and heredocs in the same command, such as
`cat > pr.md <<'EOF' ... EOF && gh pr create --body-file pr.md` or
`--body "$(cat <<'EOF' ... EOF)"`. When the body file is written by another
step of the same command, or its path depends on a shell variable, it denies
the command and asks for the body to be written first, at a literal path. When
it can't recover the body any other way, or fails for any reason, it allows the
command and writes a note to stderr. It never blocks `gh` because of its own
error. A denial says that none of the command ran, so a body file the command
would have written still holds its old content.

## Use the linter on its own

```sh
python3 plugin/scripts/pr_body_lint.py --check-file pr.md --diff-lines 120
python3 plugin/scripts/pr_body_lint.py --check-file - --json < pr.md
```

It exits 0 when the body passes and 2 when it doesn't.

## Voice

The guide writes in a neutral, plain voice. To use your own, put your voice
notes in `~/.config/pr-craft/voice.md`. `/pr-craft` reads that file when it
exists.

## Skip the checks

Set `PR_CRAFT_SKIP=1` in the environment Claude Code starts in, or prefix a
single command with it (`PR_CRAFT_SKIP=1 gh pr create ...`), for an urgent fix
or a body you want to keep as is.

## Develop

The tests need only `python3` and `git`:

```sh
python3 -I -m unittest
claude plugin validate ./plugin
```

## License

MIT
