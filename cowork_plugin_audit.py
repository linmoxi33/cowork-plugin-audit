#!/usr/bin/env python3
"""cowork-plugin-audit - audit a Claude/Cowork plugin marketplace at the install boundary.

Core idea: a plugin repository is NOT what gets installed. Installers copy only the
declared component directories (skills/, commands/, agents/, .mcp.json,
.claude-plugin/plugin.json). References that resolve fine inside the monorepo but
point outside that installed set silently break after installation.

Offline, stdlib only. Exit codes: 0 = pass, 1 = findings, 2 = usage/input error.
"""
import argparse
import json
import os
import re
import sys

__version__ = "1.0.0"

# Directories/files the documented plugin layout installs. Anything else that a
# markdown file links to is a candidate for "breaks after install".
INSTALLED_TOP = (".claude-plugin/plugin.json", ".mcp.json")
INSTALLED_DIRS = ("skills", "commands", "agents")

LINK_RE = re.compile(r"\[[^\]]*\]\(\s*([^)\s]+?)\s*\)")
FILE_TARGET_RE = re.compile(r"^[A-Za-z0-9_][^()\s]*\.(md|markdown|json|ya?ml|txt|py|sh|ts|js|toml)$", re.I)
PLACEHOLDER_RE = re.compile(r"~~[A-Za-z][A-Za-z0-9 ._/-]*")


def _findings(rule, severity, path, message, line=None, target=None):
    f = {"rule": rule, "severity": severity, "path": path, "message": message}
    if line is not None:
        f["line"] = line
    if target is not None:
        f["target"] = target
    return f


def parse_frontmatter(text):
    """Return (dict, body_start_line) for a leading '---' YAML block, else ({}, 1)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, 1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            data = {}
            for raw in lines[1:i]:
                if not raw.strip() or raw.lstrip().startswith("#"):
                    continue
                if ":" in raw:
                    k, v = raw.split(":", 1)
                    data[k.strip()] = v.strip().strip('"').strip("'")
            return data, i + 2
    return {}, 1


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), None
    except (OSError, ValueError) as exc:
        return None, str(exc)


def is_installed(plugin_root, abs_target):
    """True when a target path would survive a documented install of the plugin."""
    rel = os.path.relpath(abs_target, plugin_root).replace(os.sep, "/")
    if rel.startswith("../") or rel == "..":
        return False
    if rel in INSTALLED_TOP:
        return True
    return any(rel == d or rel.startswith(d + "/") for d in INSTALLED_DIRS)


def iter_markdown(plugin_root):
    for top in INSTALLED_DIRS:
        base = os.path.join(plugin_root, top)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in (".git", "__pycache__"))
            for name in sorted(filenames):
                if name.endswith(".md"):
                    yield os.path.join(dirpath, name)


def audit(root):
    findings = []
    src_root = root
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return None, "root is not a directory: %s" % root

    mp_path = os.path.join(root, ".claude-plugin", "marketplace.json")
    marketplace, err = load_json(mp_path)
    if marketplace is None:
        findings.append(_findings("M001", "error", ".claude-plugin/marketplace.json",
                                  "marketplace manifest missing or not valid JSON: %s" % err))
        return {"root": src_root, "summary": _summary(findings, 0, 0, 0), "findings": findings}, None
    entries = marketplace.get("plugins")
    if not isinstance(entries, list):
        findings.append(_findings("M001", "error", ".claude-plugin/marketplace.json",
                                  "'plugins' is missing or not a list"))
        entries = []

    seen = {}
    local_plugins = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            findings.append(_findings("M001", "error", ".claude-plugin/marketplace.json",
                                      "plugins[%d] is not an object" % idx))
            continue
        name = entry.get("name") or "plugins[%d]" % idx
        if name in seen:
            findings.append(_findings("M009", "error", ".claude-plugin/marketplace.json",
                                      "duplicate marketplace entry name %r" % name))
        seen[name] = True
        src = entry.get("source")
        if isinstance(src, dict):
            if not src.get("sha"):
                findings.append(_findings("M010", "warning", ".claude-plugin/marketplace.json",
                                          "remote source for %r is not pinned to a sha" % name))
            continue
        rel = src if isinstance(src, str) else None
        if not rel:
            findings.append(_findings("M001", "error", ".claude-plugin/marketplace.json",
                                      "entry %r has no usable source" % name))
            continue
        pdir = os.path.normpath(os.path.join(root, rel))
        rel = os.path.relpath(pdir, root).replace(os.sep, "/")
        local_plugins.append((name, rel, pdir))
        if not os.path.isdir(pdir):
            findings.append(_findings("M002", "error", rel,
                                      "marketplace source path for %r does not exist" % name))
            continue
        pj_rel = os.path.join(rel, ".claude-plugin", "plugin.json").replace(os.sep, "/")
        manifest, err = load_json(os.path.join(pdir, ".claude-plugin", "plugin.json"))
        if manifest is None:
            findings.append(_findings("M003" if not os.path.exists(os.path.join(pdir, ".claude-plugin", "plugin.json")) else "M004",
                                      "error", pj_rel, "plugin.json missing or invalid: %s" % err))
            continue
        if manifest.get("name") != entry.get("name"):
            findings.append(_findings("M005", "error", pj_rel,
                                      "plugin.json name %r != marketplace name %r (two writes of one identity)"
                                      % (manifest.get("name"), entry.get("name"))))
        if not manifest.get("version"):
            findings.append(_findings("M008", "warning", pj_rel, "plugin.json has no version"))
        md = (entry.get("description") or "").strip()
        pd = (manifest.get("description") or "").strip()
        if md and pd and md != pd:
            findings.append(_findings("M006", "warning", pj_rel,
                                      "description differs between marketplace.json and plugin.json (drift)"))
        findings.extend(_audit_components(root, rel, pdir))

    listed = {os.path.normpath(os.path.join(root, r)) for _, r, _ in local_plugins}
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in (".git", "__pycache__", "node_modules"))
        if ".claude-plugin" in dirnames and os.path.isfile(os.path.join(dirpath, ".claude-plugin", "plugin.json")):
            if os.path.dirname(dirpath) not in (root, os.path.join(root, "partner-built")) or \
               os.path.normpath(dirpath) not in listed:
                rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
                findings.append(_findings("M007", "warning", rel,
                                          "plugin manifest present but directory is not listed in marketplace.json"))

    skills = [f for f in _walk_components(root, "skills", "SKILL.md")]
    commands = [f for f in _walk_components(root, "commands", "*.md")]
    _cross_plugin_dupes(findings, root, skills, "S005", "skill")
    _cross_plugin_dupes(findings, root, commands, "S006", "command")
    findings.sort(key=lambda f: (f["rule"], f["path"], f.get("line", 0), f.get("target", "")))
    return {"root": src_root,
            "summary": _summary(findings, len(local_plugins), len(skills), len(commands)),
            "findings": findings}, None


def _walk_components(root, top, pattern):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in (".git", "__pycache__", "node_modules"))
        parts = dirpath.split(os.sep)
        if top not in parts:
            continue
        if top == "skills" and os.path.basename(dirpath) == "skills":
            continue
        for name in sorted(filenames):
            if pattern == "SKILL.md" and name == "SKILL.md":
                out.append(os.path.join(dirpath, name))
            elif pattern == "*.md" and name.endswith(".md"):
                out.append(os.path.join(dirpath, name))
    return out


def _cross_plugin_dupes(findings, root, files, rule, kind):
    owners = {}
    for path in files:
        parts = path.split(os.sep)
        try:
            i = parts.index("skills" if kind == "skill" else "commands")
        except ValueError:
            continue
        plugin = os.sep.join(parts[:i])
        stem = os.path.basename(os.path.dirname(path))
        owners.setdefault(stem, set()).add(plugin)
    for stem, plisted in sorted(owners.items()):
        if len(plisted) > 1:
            rel = os.path.relpath(sorted(plisted)[0], root).replace(os.sep, "/")
            findings.append(_findings(rule, "warning", rel,
                                      "%s name %r is defined in %d plugins; the loader keeps one silently"
                                      % (kind, stem, len(plisted))))


def _audit_components(root, plugin_rel, pdir):
    out = []
    for path in iter_markdown(pdir):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            out.append(_findings("M004", "error", rel, "unreadable: %s" % exc))
            continue
        base = os.path.basename(path)
        fm, _body = parse_frontmatter(text)
        if base == "SKILL.md":
            if not fm and not text.startswith("---"):
                out.append(_findings("S001", "error", rel, "SKILL.md has no YAML frontmatter"))
            if not fm.get("name"):
                out.append(_findings("S002", "error", rel, "frontmatter has no name"))
            elif fm["name"] != os.path.basename(os.path.dirname(path)):
                out.append(_findings("S003", "warning", rel,
                                     "frontmatter name %r != directory %r"
                                     % (fm["name"], os.path.basename(os.path.dirname(path)))))
            if not fm.get("description"):
                out.append(_findings("S004", "warning", rel, "frontmatter has no description"))
        in_fence = False
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for target in LINK_RE.findall(line):
                if "://" in target or target.startswith(("mailto:", "#", "/")):
                    continue
                tgt = target.split("#", 1)[0]
                if not tgt or not (tgt.startswith(("./", "../")) or FILE_TARGET_RE.match(tgt)):
                    continue
                abs_t = os.path.normpath(os.path.join(os.path.dirname(path), tgt))
                if not os.path.exists(abs_t):
                    out.append(_findings("L002", "error", rel,
                                         "relative link target does not exist anywhere in the repo",
                                         line=lineno, target=tgt))
                elif not is_installed(pdir, abs_t):
                    out.append(_findings("L001", "error", rel,
                                         "link resolves in the repo but its target is outside the installed "
                                         "component set, so it breaks after install",
                                         line=lineno, target=tgt))
            if PLACEHOLDER_RE.search(line):
                out.append(_findings("P001", "info", rel,
                                     "unresolved '~~' placeholder token in an installed file",
                                     line=lineno))
    return out


def _summary(findings, plugins, skills, commands):
    by_rule = {}
    for f in findings:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    return {
        "plugins_audited": plugins,
        "skills": skills,
        "commands": commands,
        "findings": len(findings),
        "by_severity": {s: sum(1 for f in findings if f["severity"] == s)
                        for s in ("error", "warning", "info")},
        "by_rule": dict(sorted(by_rule.items())),
    }


def render_text(report):
    s = report["summary"]
    lines = ["cowork-plugin-audit: %s" % report["root"],
             "plugins=%d skills=%d commands=%d findings=%d (error=%d warning=%d info=%d)"
             % (s["plugins_audited"], s["skills"], s["commands"], s["findings"],
                s["by_severity"]["error"], s["by_severity"]["warning"], s["by_severity"]["info"])]
    for f in report["findings"]:
        loc = f["path"] + (":%d" % f["line"] if "line" in f else "")
        extra = " -> %s" % f["target"] if "target" in f else ""
        lines.append("%-5s %-8s %s: %s%s" % (f["severity"], f["rule"], loc, f["message"], extra))
    if not report["findings"]:
        lines.append("OK: no findings")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cowork-plugin-audit",
        description="Audit a Claude/Cowork plugin marketplace for install-boundary breakage "
                    "and manifest drift.")
    parser.add_argument("root", nargs="?", default=".",
                        help="path to the plugin marketplace repository")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit a machine-readable JSON report on stdout")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = parser.parse_args(argv)

    if not os.path.isdir(args.root):
        sys.stderr.write("error: %s is not a directory\n" % args.root)
        return 2
    if not os.path.exists(os.path.join(args.root, ".claude-plugin", "marketplace.json")):
        sys.stderr.write("error: no .claude-plugin/marketplace.json under %s\n" % args.root)
        return 2

    report, err = audit(args.root)
    if err is not None:
        sys.stderr.write("error: %s\n" % err)
        return 2
    if args.as_json:
        json.dump(report, sys.stdout, indent=2, sort_keys=False)
        sys.stdout.write("\n")
    else:
        print(render_text(report))
    blocking = report["summary"]["by_severity"]["error"] + report["summary"]["by_severity"]["warning"]
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
