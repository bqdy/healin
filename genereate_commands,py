#!/usr/bin/env python3
"""Builds commands.json for the website from your bot's cogs.

Usage:  python generate_commands.py [path/to/cogs] [output.json]
Run it again whenever you add or change commands, then redeploy.
It reads each cog's hand-written `help_text` (the same text -help shows) and the
category order from cogs/general.py. It never imports or runs your bot.
"""
import ast, glob, json, os, re, sys

COGS = sys.argv[1] if len(sys.argv) > 1 else "cogs"
OUT = sys.argv[2] if len(sys.argv) > 2 else "commands.json"
BOT_NAME = os.getenv("BOT_NAME", "Heal")
PREFIX = os.getenv("PREFIX", "-")

def lit(n):
    try: return ast.literal_eval(n)
    except Exception: return None

def ev(node, ns):
    return eval(compile(ast.Expression(node), "x", "eval"), ns)

def read_cogs():
    found = {}
    for path in sorted(glob.glob(os.path.join(COGS, "*.py"))):
        tree = ast.parse(open(path, encoding="utf-8").read())
        ns = {}
        for n in tree.body:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                try: ns[n.targets[0].id] = ev(n.value, dict(ns))
                except Exception: pass
            elif isinstance(n, ast.FunctionDef):
                try: exec(compile(ast.Module([n], []), "x", "exec"), ns)
                except Exception: pass
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            name = None
            for kw in cls.keywords:
                if kw.arg == "name": name = lit(kw.value)
            cns, pages = dict(ns), None
            for n in cls.body:
                if not (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)):
                    continue
                key = n.targets[0].id
                try: val = ev(n.value, cns)
                except Exception: continue
                if key == "help_text":
                    pages = [val] if isinstance(val, str) else list(val)
                else:
                    cns[key] = val
            if pages:
                found[name or cls.name] = pages
    return found

def category_order():
    src = os.path.join(COGS, "general.py")
    if os.path.exists(src):
        for n in ast.parse(open(src, encoding="utf-8").read()).body:
            if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "CATEGORIES" for t in n.targets):
                v = lit(n.value)
                if v: return v
    return None

LEAD = re.compile(r"^((?:`[^`]*`\s*(?:/\s*)?)+)(.*)$")

def parse_command(line):
    """A help line is one or more `code` spans (usually `{p}name args`), optionally followed by ' - description'.
    Returns {"usages": [...], "desc": "..."} or None if the line is not a command."""
    if not line.startswith("`{p}"): return None
    m = LEAD.match(line)
    if not m: return None
    spans = re.findall(r"`([^`]*)`", m.group(1))
    usages = [sp.replace("{p}", "").strip() for sp in spans]
    # a trailing "@user"-style span after a run of commands is a shared argument, not its own command
    if len(usages) > 2 and usages[-1][:1] in "@<[":
        arg = usages.pop(); usages = [u + " " + arg for u in usages]
    rest = m.group(2).strip()
    desc = rest[1:].strip() if rest.startswith("-") else rest
    return {"usages": usages, "desc": desc}

def parse(pages):
    intro, sections, cur, required = [], [], None, None
    def section(title):
        s = {"title": title, "required": None, "notes": [], "commands": []}
        sections.append(s); return s
    for page in pages:
        for raw in page.replace("\r", "").split("\n"):
            line = raw.strip()
            if not line or re.match(r"^# ", line): continue
            if line.startswith("## "):
                cur = section(line[3:].strip()); continue
            m = re.match(r"^\*\*([^*]+):\*\*\s*(.*)$", line)
            if m and m.group(1).strip().lower() in ("required", "management"):
                if cur is None: required = m.group(2).strip()
                else: cur["required"] = m.group(2).strip()
                continue
            c = parse_command(line)
            if c:
                if cur is None: cur = section("Commands")
                cur["commands"].append(c)
                continue
            (cur["notes"] if cur else intro).append(line)
    # merge sections with the same title (multi-page categories)
    merged = {}
    for s in sections:
        t = merged.setdefault(s["title"], {"title": s["title"], "required": None, "notes": [], "commands": []})
        t["required"] = t["required"] or s["required"]
        t["notes"] += s["notes"]; t["commands"] += s["commands"]
    return intro, required, [s for s in merged.values() if s["commands"] or s["notes"]]

def slug(s): return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

cogs = read_cogs()
order = category_order() or sorted(cogs)
cats = []
for name in order:
    if name not in cogs: continue
    intro, required, sections = parse(cogs[name])
    cats.append({"name": name, "slug": slug(name), "intro": intro, "required": required, "sections": sections,
                 "count": sum(len(s["commands"]) for s in sections)})
data = {"bot": BOT_NAME, "prefix": PREFIX, "categories": cats}
json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"{sum(c['count'] for c in cats)} commands in {len(cats)} categories -> {OUT}")
