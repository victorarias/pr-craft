# pr-craft

A Claude Code plugin for pull request descriptions that reviewers can read. A
`PreToolUse` hook denies `gh pr create` and `gh pr edit` when the body breaks a
rule and tells the agent what to fix. `/pr-craft` is a writing guide you invoke
by hand.

## Install

```text
/plugin install pr-craft --marketplace victorarias/pr-craft
```

On older Claude Code versions:

```text
/plugin marketplace add victorarias/pr-craft
/plugin install pr-craft@pr-craft
```

The rules, the voice file (`~/.config/pr-craft/voice.md`), the
`PR_CRAFT_SKIP=1` escape hatch and standalone linter usage are described at
https://github.com/victorarias/pr-craft.
