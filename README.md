# cowork-plugin-audit

**A marketplace can be perfectly green in CI and still ship broken plugins: the repository is not the artifact that gets installed.** `cowork-plugin-audit` resolves every relative reference inside a Claude/Cowork plugin marketplace against the *installed* component set, so links that work on GitHub but 404 after `claude plugin install` become CI-blocking findings.

Upstream: https://github.com/anthropics/knowledge-work-plugins

## What the upstream is, and the real gap it has

[`anthropics/knowledge-work-plugins`](https://github.com/anthropics/knowledge-work-plugins) is Anthropic's open-source marketplace of 11+ role plugins (plus partner-built entries) for Claude Cowork / Claude Code. Its README fixes the installable layout:

```
plugin-name/
├── .claude-plugin/plugin.json
├── .mcp.json
├── commands/
└── skills/
```

Everything else in a plugin directory is **not** part of that layout. Yet skill files link into that non-installed space:

- Issue [#1381](https://github.com/anthropics/knowledge-work-plugins/issues/1381) — *"Skills link to ../../CONNECTORS.md, which isn't installed when plugins are added on claude.ai"*; the follow-up PR [#1389](https://github.com/anthropics/knowledge-work-plugins/pull/1389) proposes documenting the convention inline because the link does not survive installation.
- Issue [#933](https://github.com/anthropics/knowledge-work-plugins/issues/933) (has a reaction) — connectors are declared in one place and described in another, so the two writes disagree.
- Issue [#71](https://github.com/anthropics/knowledge-work-plugins/issues/71) — `~~placeholder` strings reach users verbatim.
- Issue [#1382](https://github.com/anthropics/knowledge-work-plugins/issues/1382) — skill descriptions overlap across plugins and nothing decides which one fires.

Nothing in the repo checks any of this, because a normal link checker resolves against the **repo** root, where `../../CONNECTORS.md` exists. Two failure classes hide there, and both are only visible if you model the install boundary:

1. **silent degradation** — a link that resolves in the monorepo but whose target is outside the installed set.
2. **double-write drift** — the same identity (`name`, `description`) written in both `marketplace.json` and `plugin.json`.

## What this tool does / does not do

Does, offline and with zero dependencies:

- resolves every markdown link in `skills/`, `commands/`, `agents/` against the **simulated installed layout** and splits findings into `L001` (resolves in repo, breaks after install) and `L002` (dangling everywhere);
- cross-checks `marketplace.json` ↔ each local `plugin.json` (name/description drift, missing version, missing source path, unpinned remote `sha`);
- flags plugin directories carrying a manifest that the marketplace never lists (`M007`);
- finds the same skill/command name defined by **two different plugins** — ownership is ambiguous and the descriptions compete for one trigger (`S005`/`S006`, the same class the upstream reports in [#1382](https://github.com/anthropics/knowledge-work-plugins/issues/1382));
- flags `~~placeholder` tokens as `info` (never changes the exit code).

Does not: fetch anything, follow remote `git-subdir` sources, validate MCP server behaviour, or claim to fix upstream.

## Install / usage

Python 3.8+, standard library only. No install step.

```bash
python3 cowork_plugin_audit.py examples/in/acme-marketplace
```

Exit code is `1` because the fixture contains planted findings. Output:

```text
cowork-plugin-audit: examples/in/acme-marketplace
plugins=2 skills=2 commands=1 findings=7 (error=2 warning=4 info=1)
error L001     alpha/skills/thing/SKILL.md:7: link resolves in the repo but its target is outside the installed component set, so it breaks after install -> ../../CONNECTORS.md
error L002     alpha/skills/thing/SKILL.md:8: relative link target does not exist anywhere in the repo -> ./missing.md
warning M006     beta/.claude-plugin/plugin.json: description differs between marketplace.json and plugin.json (drift)
warning M007     gamma: plugin manifest present but directory is not listed in marketplace.json
warning M008     beta/.claude-plugin/plugin.json: plugin.json has no version
info  P001     alpha/skills/thing/SKILL.md:9: unresolved '~~' placeholder token in an installed file
warning S005     alpha: skill name 'thing' is defined in 2 plugins; ownership is ambiguous and the two descriptions compete for the same trigger
```

Machine-readable output (`examples/out/report.json` is the committed expected file):

```bash
python3 cowork_plugin_audit.py examples/in/acme-marketplace --json | python3 -c "import json,sys;print(json.load(sys.stdin)['summary']['by_rule'])"
# {'L001': 1, 'L002': 1, 'M006': 1, 'M007': 1, 'M008': 1, 'P001': 1, 'S005': 1}
```

Against the upstream repository at `2574e29377c7173d0cecffef29a6ecbd7f8ccb5d` (252 `SKILL.md`, 22 locally-audited plugins):

```console
$ python3 cowork_plugin_audit.py /path/to/knowledge-work-plugins --json
{"plugins_audited": 22, "skills": 252, "commands": 15, "findings": 350,
 "by_severity": {"error": 104, "warning": 54, "info": 192},
 "by_rule": {"L001": 104, "M006": 6, "P001": 192, "S003": 41, "S005": 6, "S006": 1}}
```

104 install-boundary breaks, 75 of them the `../../CONNECTORS.md` class from issue #1381.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | pass: no `error`/`warning` findings (`info` alone does not fail) |
| `1` | findings detected (at least one `error` or `warning`) |
| `2` | usage or input error (root not a directory, no `.claude-plugin/marketplace.json`) |

### Rules

| Rule | Severity | Check |
| --- | --- | --- |
| M001 | error | marketplace manifest missing/invalid |
| M002 | error | local plugin source path does not exist |
| M003 / M004 | error | `plugin.json` missing / invalid |
| M005 | error | `plugin.json` name ≠ marketplace name |
| M006 | warning | description drifts between the two manifests |
| M007 | warning | plugin manifest present but not listed in the marketplace |
| M008 | warning | `plugin.json` has no version |
| M009 | error | duplicate marketplace entry name |
| M010 | warning | remote source not pinned to a `sha` |
| S001 / S002 | error | `SKILL.md` frontmatter missing / has no name |
| S003 | warning | frontmatter name ≠ directory name |
| S004 | warning | frontmatter has no description |
| S005 / S006 | warning | same skill/command name in two plugins (ambiguous ownership) |
| L001 | error | link resolves in repo but breaks after install |
| L002 | error | link target missing everywhere |
| P001 | info | unresolved `~~` placeholder in an installed file |

## Tests

```bash
python3 -m unittest discover -s tests -v   # 21 tests, 12 behavioural
```

## 本轮创新

**First check that resolves plugin links against the simulated installed layout instead of the repository root.** Existing link checkers (and the upstream's own CI) see `../../CONNECTORS.md` as valid, because the file exists in the monorepo; this tool models what the installer actually copies and therefore reports the 104 references that can never resolve after `claude plugin install` — then merges that "silent degradation" chain with "double-write drift" (`marketplace.json` vs `plugin.json`) and cross-plugin name-ownership ambiguity into one rule chain. The number 104 is a new, reproducible reading about the upstream, not a restatement of its README.

## 与同类项目的差异

- [`superpowers-skill-audit`](https://github.com/linmoxi33/superpowers-skill-audit) inspects one skill package's frontmatter, broken links and orphan files against the source tree. It cannot see install-boundary breakage because its reference point is the repo, not the installed artifact, and it has no marketplace manifest graph.
- [`impeccable-install-audit`](https://github.com/linmoxi33/impeccable-install-audit) compares installed *copies* by sha256 across harnesses; it detects divergence after installation. This tool predicts breakage *before* installation and does not hash files.
- Generic markdown link checkers report zero problems on this repository for the `L001` class, because in the repo those targets exist.

## Limitations

- Only local (`"source": "./dir"`) plugins are audited; `git-subdir` entries are counted and checked for a pinned `sha`, but their contents are not fetched.
- Link detection covers markdown `[text](target)` targets that look like file paths. Links inside fenced code blocks and absolute `/site/path` URLs are ignored, and `@file`-style references are not parsed.
- The installed set is taken from the documented layout in the upstream README (`.claude-plugin/plugin.json`, `.mcp.json`, `skills/`, `commands/`, `agents/`). If an installer copies more, `L001` over-reports; the rule id is stable and easy to filter.
- `P001` reflects the upstream's placeholder convention and is informational by design.
