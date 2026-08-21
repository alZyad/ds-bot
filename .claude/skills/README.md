# Vendored skills — mattpocock/skills

Matt Pocock's agent skills, copied into this repository so they are available to
every checkout without a per-machine install step.

* Upstream: <https://github.com/mattpocock/skills>
* Package: `mattpocock-skills` v1.2.3
* Commit: `163b780f98079010cfaff187cfcb9cb24f31998f`
* Licence: MIT — see [LICENSE](LICENSE) (Copyright (c) 2026 Matt Pocock)

## What is here

The 25 skills the upstream plugin manifest (`.claude-plugin/plugin.json`)
declares as released, copied verbatim, one directory each:

| Engineering | Productivity |
| --- | --- |
| ask-matt, code-review, codebase-design, diagnosing-bugs, domain-modeling, grill-with-docs, implement, improve-codebase-architecture, prototype, research, resolving-merge-conflicts, setup-matt-pocock-skills, tdd, to-spec, to-tickets, triage, wayfinder, wizard | grill-me, grilling, handoff, teach, to-questionnaire, wait-what, writing-for-agents |

Upstream's `skills/misc`, `skills/in-progress` and `skills/deprecated`
directories are **not** installed: the manifest does not ship them. Copy a
directory from upstream into `.claude/skills/` if you want one of those.

Each skill keeps its own supporting files (`tdd/tests.md`,
`codebase-design/DEEPENING.md`, the per-skill `agents/openai.yaml`, …). The
released skills reference nothing outside their own directory, so the copies work
standalone.

## Before first use

Upstream asks you to run `/setup-matt-pocock-skills` once per repository, to
point the skills at this project's issue tracker, triage labels and docs
location. That has **not** been run here yet — do it in an interactive session.

## Updating

These are plain copies, not symlinks, so `npx skills@latest update` will not
touch them. To refresh:

```bash
git clone --depth 1 https://github.com/mattpocock/skills.git /tmp/mp-skills
# copy each directory named in /tmp/mp-skills/.claude-plugin/plugin.json
# over the matching directory here, then update the commit hash above
```

Alternatively drop this directory and install per-machine instead, with
`/plugin install mattpocock-skills` (Claude Code) or
`npx skills@latest add mattpocock/skills`.
