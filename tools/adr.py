#!/usr/bin/env python3
"""adr.py - deterministic ADR log operations for agents (stdlib only).

Subcommands
  init       [--dir D] [--owner O]                     create log, 0001 bootstrap ADR, index
  new        --title T [--template nygard|aws] [--owner O]
             [--supersedes N ...] [--amends N ...] [--placeholder]
                                                       create Proposed ADR, print path
  renumber   FILE                                      XXXX-*.md -> next free number
  accept     N [--stakeholders a,b] [--version 1.0]    Proposed -> Accepted; finalize supersede/amend links
  reject     N --reason R                              Proposed -> Rejected with reason
  deprecate  N --reason R                              Accepted -> Deprecated
  list       [--status S] [--json]                     list ADRs (for preflight)
  index                                                regenerate README.md
  lint       [--base GIT_REF] [--strict]               validate log; exit 1 on errors

Global: --dir (default: docs/adr, or doc/adr / doc/architecture/decisions if present, or $ADR_DIR)
"""
import argparse, datetime as dt, json, os, re, subprocess, sys
from pathlib import Path

STATUSES = ("Proposed", "Accepted", "Rejected", "Superseded", "Deprecated")
IMMUTABLE = ("Accepted", "Rejected", "Superseded", "Deprecated")
FNAME_RE = re.compile(r"^(\d{4})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
def _template_dir():
    # ponytail: skill layout is <skill>/templates; the repo copy is tools/adr.py.
    # Ceiling: these two paths. Upgrade path: a --templates flag in adr-steward.
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / "templates", here.parent / "docs" / "adr" / "templates"):
        if (candidate / "nygard.md").is_file():
            return candidate
    return here.parent / "docs" / "adr" / "templates"


TEMPLATES = _template_dir()
TODAY = dt.date.today().isoformat()


# ---------------------------------------------------------------- helpers
def find_dir(arg):
    if arg:
        return Path(arg)
    if os.environ.get("ADR_DIR"):
        return Path(os.environ["ADR_DIR"])
    for c in ("docs/adr", "doc/adr", "doc/architecture/decisions", "docs/decisions"):
        if Path(c).is_dir():
            return Path(c)
    return Path("docs/adr")


def slugify(title):
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)[:80].strip("-")


def split_frontmatter(text):
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[4:end], text[end + 5:]
    return None, text


def parse_fm(fm):
    data = {}
    for line in (fm or "").splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*?)\s*(?:#.*)?$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2)
        if v.startswith("[") and v.endswith("]"):
            v = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        elif v in ("null", "~", ""):
            v = None
        data[k] = v
    return data


def set_fm(text, key, value):
    fm, body = split_frontmatter(text)
    if fm is None:
        return text
    if isinstance(value, list):
        value = "[" + ", ".join(value) + "]"
    value = "null" if value is None else str(value)
    pat = re.compile(rf"^{re.escape(key)}:.*$", re.M)
    if pat.search(fm):
        fm = pat.sub(lambda _: f"{key}: {value}", fm, count=1)
    else:
        fm += f"\n{key}: {value}"
    return f"---\n{fm}\n---\n{body}"


def sections(body):
    """Return {heading_lower: text} for level-2 headings."""
    out, cur, buf = {}, None, []
    for line in body.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and not line.startswith("###"):
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1).strip().lower(), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out


def strip_comments(s):
    return re.sub(r"<!--.*?-->", "", s or "", flags=re.S).strip()


class ADR:
    def __init__(self, path):
        self.path = Path(path)
        self.text = self.path.read_text(encoding="utf-8")
        self.reload()

    def reload(self):
        fm, self.body = split_frontmatter(self.text)
        self.fm = parse_fm(fm) if fm is not None else None
        m = FNAME_RE.match(self.path.name)
        self.number = int(m.group(1)) if m else None
        h = re.search(r"^#\s+(?:\d+\.\s*|\d+\s+-\s+)?(.+?)\s*$", self.body, re.M)
        self.title = h.group(1) if h else self.path.stem
        self.secs = sections(self.body)
        self.status_lines = [l.strip() for l in self.secs.get("status", "").splitlines()
                             if l.strip() and not l.strip().startswith("<!--")]

    @property
    def status(self):
        for l in self.status_lines:
            for s in ("Superseded", "Deprecated"):
                if l.startswith(s):
                    return s
        if self.status_lines:
            first = self.status_lines[0].split()[0].rstrip(".,:")
            return first
        if self.fm and self.fm.get("status"):
            return self.fm["status"]
        return None

    def links(self, kind):
        """Numbers linked by a relation line, e.g. kind='Supersedes'."""
        nums = set()
        for l in self.status_lines:
            if l.startswith(kind):
                nums |= {int(n) for n in re.findall(r"\b(\d{4})\b", l)}
        if self.fm:
            key = {"Supersedes": "supersedes", "Superseded by": "superseded_by",
                   "Amends": "amends"}.get(kind)
            v = self.fm.get(key) if key else None
            for x in (v if isinstance(v, list) else [v] if v else []):
                if str(x).isdigit():
                    nums.add(int(x))
        return nums

    def save(self):
        self.path.write_text(self.text, encoding="utf-8")
        self.reload()

    # status-section editing -------------------------------------------
    def _replace_status_block(self, new_lines):
        pat = re.compile(r"(^##[ \t]+Status[ \t]*\n)(.*?)(?=^##\s|\Z)", re.M | re.S)
        block = "\n" + "\n".join(new_lines) + "\n\n"
        if pat.search(self.text):
            self.text = pat.sub(lambda m: m.group(1) + block, self.text, count=1)
        self.reload()

    def set_primary_status(self, line):
        lines = [l for l in self.status_lines if not l.startswith(("Proposed",))]
        if line.startswith(("Accepted", "Rejected")):
            lines = [line] + [l for l in lines if not l.startswith(("Accepted", "Rejected"))]
        else:  # Superseded / Deprecated go on top, keep Accepted history line
            lines = [line] + lines
        self._replace_status_block(lines)
        if self.fm is not None:
            self.text = set_fm(self.text, "status", line.split()[0])
            self.reload()

    def add_relation(self, line):
        if line not in self.status_lines:
            self._replace_status_block(self.status_lines + [line])

    def add_history(self, version, author, change):
        row = f"| {version} | {TODAY} | {author} | {change} |"
        if re.search(r"^##\s+Change History", self.text, re.M):
            self.text = self.text.rstrip("\n") + "\n" + row + "\n"
        else:
            self.text = (self.text.rstrip("\n") + "\n\n## Change History\n\n"
                         "| Version | Date | Author | Change |\n|---|---|---|---|\n" + row + "\n")
        self.reload()


def load_all(d):
    return sorted((ADR(p) for p in d.glob("*.md") if FNAME_RE.match(p.name)),
                  key=lambda a: a.number)


def by_number(d, n):
    for a in load_all(d):
        if a.number == int(n):
            return a
    sys.exit(f"error: ADR {int(n):04d} not found in {d}")


def next_number(d):
    nums = [a.number for a in load_all(d)]
    return (max(nums) + 1) if nums else 1


def rel_link(a):
    return f"[{a.number:04d}. {a.title}]({a.path.name})"


def render(template, **kw):
    t = (TEMPLATES / template).read_text(encoding="utf-8")
    for k, v in kw.items():
        t = t.replace("{{" + k + "}}", v)
    return re.sub(r"\n{3,}", "\n\n", t)


# ---------------------------------------------------------------- commands
def cmd_init(a, d):
    d.mkdir(parents=True, exist_ok=True)
    p = d / "0001-record-architecture-decisions.md"
    if not p.exists():
        p.write_text(render("init.md", DATE=TODAY, OWNER=a.owner), encoding="utf-8")
    write_index(d)
    print(p)


def cmd_new(a, d):
    tpl = {"nygard": "nygard.md", "aws": "aws.md"}[a.template]
    rel = []
    for n in a.supersedes or []:
        rel.append("Supersedes " + rel_link(by_number(d, n)))
    for n in a.amends or []:
        rel.append("Amends " + rel_link(by_number(d, n)))
    num = "XXXX" if a.placeholder else f"{next_number(d):04d}"
    text = render(tpl, NUMBER=num, TITLE=a.title, DATE=TODAY, OWNER=a.owner,
                  RELATIONS="\n".join(rel),
                  SUPERSEDES=", ".join(f"{int(n):04d}" for n in a.supersedes or []),
                  AMENDS=", ".join(f"{int(n):04d}" for n in a.amends or []))
    p = d / f"{num}-{slugify(a.title)}.md"
    if p.exists():
        sys.exit(f"error: {p} exists")
    d.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    if not a.placeholder:
        write_index(d)
    print(p)


def cmd_renumber(a, d):
    src = Path(a.file)
    if not src.name.startswith("XXXX-"):
        sys.exit("error: only XXXX-*.md placeholder files can be renumbered")
    n = f"{next_number(d):04d}"
    text = src.read_text(encoding="utf-8").replace("XXXX", n)
    dst = src.with_name(n + src.name[4:])
    dst.write_text(text, encoding="utf-8")
    src.unlink()
    write_index(d)
    print(dst)


def cmd_accept(a, d):
    adr = by_number(d, a.number)
    if adr.status != "Proposed":
        sys.exit(f"error: ADR {adr.number:04d} is {adr.status}, only Proposed can be accepted")
    empty = [s for s in ("context", "decision", "consequences") if not strip_comments(adr.secs.get(s))]
    if empty and not a.force:
        sys.exit(f"error: ADR {adr.number:04d} has empty sections {empty}; complete them first (or --force)")
    if re.search(r"<[a-z][^>]*>", strip_comments(adr.secs.get("decision", ""))) and not a.force:
        sys.exit(f"error: ADR {adr.number:04d} Decision still contains <placeholders> (or --force)")
    adr.set_primary_status(f"Accepted {TODAY} (v{a.version})")
    if adr.fm is not None:
        adr.text = set_fm(adr.text, "accepted", TODAY)
        adr.text = set_fm(adr.text, "version", a.version)
        if a.stakeholders:
            adr.text = set_fm(adr.text, "stakeholders", a.stakeholders.split(","))
        adr.reload()
    old_targets = adr.links("Supersedes")
    adr.add_history(a.version, a.owner, "Accepted" + (
        f"; supersedes {', '.join(f'{n:04d}' for n in sorted(old_targets))}" if old_targets else ""))
    adr.save()
    for n in sorted(old_targets):
        old = by_number(d, n)
        old.set_primary_status("Superseded by " + rel_link(adr))
        if old.fm is not None:
            old.text = set_fm(old.text, "superseded_by", f"{adr.number:04d}")
            old.reload()
        old.add_history("-", a.owner, f"Superseded by {adr.number:04d}")
        old.save()
    for n in sorted(adr.links("Amends")):
        tgt = by_number(d, n)
        tgt.add_relation("Amended by " + rel_link(adr))
        tgt.add_history("-", a.owner, f"Amended by {adr.number:04d}")
        tgt.save()
    write_index(d)
    print(adr.path)


def _close(a, d, new_status, allowed_from):
    adr = by_number(d, a.number)
    if adr.status not in allowed_from:
        sys.exit(f"error: ADR {adr.number:04d} is {adr.status}; expected {allowed_from}")
    adr.set_primary_status(f"{new_status} {TODAY}")
    heading = "Rejection Reason" if new_status == "Rejected" else "Deprecation Reason"
    adr.text = adr.text.rstrip("\n") + f"\n\n## {heading}\n\n{a.reason}\n"
    adr.reload()
    adr.add_history("-", a.owner, new_status)
    adr.save()
    write_index(d)
    print(adr.path)


def cmd_reject(a, d):
    _close(a, d, "Rejected", ("Proposed",))


def cmd_deprecate(a, d):
    _close(a, d, "Deprecated", ("Accepted",))


def cmd_list(a, d):
    rows = []
    for x in load_all(d):
        if a.status and x.status != a.status:
            continue
        rows.append({"number": f"{x.number:04d}", "title": x.title, "status": x.status,
                     "file": str(x.path), "tags": (x.fm or {}).get("tags") or [],
                     "summary_line": x.status_lines[0] if x.status_lines else ""})
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        for r in rows:
            print(f"{r['number']}  {r['status']:<11} {r['title']}")


def index_text(d):
    lines = ["# Architecture Decision Log", "",
             "<!-- Generated by tools/adr.py index. Do not edit by hand. -->", "",
             "| # | Title | Status | Date |", "|---|---|---|---|"]
    for x in load_all(d):
        st = " ".join(x.status_lines) if x.status_lines else (x.status or "")
        st = re.sub(r"\[(\d{4})\.[^\]]*\]\(([^)]+)\)", r"[\1](\2)", st)
        date = re.search(r"^Date:\s*(\S+)", x.body, re.M)
        date = date.group(1) if date else (x.fm or {}).get("created") or ""
        lines.append(f"| {x.number:04d} | [{x.title}]({x.path.name}) | {st} | {date} |")
    return "\n".join(lines) + "\n"


def write_index(d):
    (d / "README.md").write_text(index_text(d), encoding="utf-8")


def cmd_index(a, d):
    write_index(d)
    print(d / "README.md")


# ---------------------------------------------------------------- lint
def git_show(ref, path):
    try:
        # ponytail: Windows default cp1252 cannot read the log. Ceiling: utf-8.
        # Upgrade path: adr-steward sets encoding="utf-8" upstream.
        return subprocess.run(
            ["git", "show", f"{ref}:{path}"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=True).stdout
    except Exception:
        return None


def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _founder_wording_pass(old, new):
    """ADR-0016. The founder allowed one wording pass on records whose Decision
    did not yet say 'We will': context stays byte-for-byte, and the old Decision
    and Consequences text stay inside the new sections. Once the base Decision
    already contains 'we will', this returns False and L5 is unconditional."""
    if old.secs.get("context") != new.secs.get("context"):
        return False
    old_dec = strip_comments(old.secs.get("decision"))
    new_dec = strip_comments(new.secs.get("decision"))
    if "we will" in old_dec.lower() or "we will" not in new_dec.lower():
        return False
    if _norm(old_dec) not in _norm(new_dec):
        return False
    old_con = strip_comments(old.secs.get("consequences"))
    new_con = strip_comments(new.secs.get("consequences"))
    return _norm(old_con) in _norm(new_con)


def cmd_lint(a, d):
    errs, warns = [], []
    E = lambda f, m: errs.append(f"ERROR {f}: {m}")
    W = lambda f, m: warns.append(f"WARN  {f}: {m}")
    files = sorted(p for p in d.glob("*.md") if p.name != "README.md")
    adrs = {}
    for p in files:
        if p.name.startswith("XXXX-"):
            E(p.name, "placeholder number must be renumbered before merge (L6)")
            continue
        if not FNAME_RE.match(p.name):
            E(p.name, "filename must match NNNN-kebab-title.md (L1)")
            continue
        x = ADR(p)
        if x.number in adrs:
            E(p.name, f"duplicate number {x.number:04d} (L1)")
        adrs[x.number] = x
    nums = sorted(adrs)
    if nums and nums != list(range(1, nums[-1] + 1)):
        W("log", f"numbering has gaps: {sorted(set(range(1, nums[-1] + 1)) - set(nums))} (L1)")
    for n, x in adrs.items():
        f = x.path.name
        for req in ("status", "context", "decision", "consequences"):
            if req not in x.secs:
                E(f, f"missing section '## {req.title()}' (L2)")
        if x.status not in STATUSES:
            E(f, f"invalid status '{x.status}' (L3)")
        if x.fm is not None and x.fm.get("status") and x.fm["status"] != x.status:
            E(f, f"frontmatter status '{x.fm['status']}' != Status section '{x.status}' (L3)")
        for t in x.links("Superseded by"):
            new = adrs.get(t)
            if not new:
                E(f, f"superseded by missing ADR {t:04d} (L4)")
            elif new.status != "Accepted" and new.status != "Superseded":
                E(f, f"superseded by {t:04d} which is {new.status}, not Accepted (L4)")
            elif n not in new.links("Supersedes"):
                E(f, f"{t:04d} lacks back-link 'Supersedes {n:04d}' (L4)")
        for t in x.links("Supersedes"):
            old = adrs.get(t)
            if not old:
                E(f, f"supersedes missing ADR {t:04d} (L4)")
            elif x.status == "Accepted" and n not in old.links("Superseded by"):
                E(f, f"accepted but {t:04d} not marked 'Superseded by {n:04d}' (L4)")
            elif x.status == "Proposed" and old.status == "Superseded" and n in old.links("Superseded by"):
                E(f, f"{t:04d} marked superseded before this ADR was accepted (L4)")
        dec = strip_comments(x.secs.get("decision"))
        if dec and not re.search(r"\bwe will\b", dec, re.I):
            W(f, "Decision should contain 'We will ...' (L7)")
        if not dec:
            (E if x.status == "Accepted" else W)(f, "Decision section is empty")
        con = strip_comments(x.body.split("## Consequences", 1)[-1]) if "consequences" in x.secs else ""
        neg = re.search(r"(?im)^###\s*Negative\s*\n+\s*-\s*\S", con) or \
            re.search(r"(?i)\b(negative|bad,|downside|worse|harder|cost|risk|trade-?off)\b", con)
        if not neg:
            W(f, "no negative consequence found (L8)")
        words = len(re.findall(r"\w+", strip_comments(x.body)))
        if words > 1200:
            W(f, f"{words} words; keep ADRs to one or two pages (L9)")
        rt = (x.fm or {}).get("review_trigger")
        if isinstance(rt, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", rt) and rt < TODAY \
                and x.status == "Accepted":
            W(f, f"review_trigger date {rt} has passed (L11)")
        if a.base:
            old_txt = git_show(a.base, x.path.as_posix())
            if old_txt:
                o = ADR.__new__(ADR); o.path = x.path; o.text = old_txt; o.reload()
                if o.status in IMMUTABLE:
                    changed = [
                        s for s in ("context", "decision", "consequences")
                        if strip_comments(o.secs.get(s)) != strip_comments(x.secs.get(s))
                    ]
                    if changed and not _founder_wording_pass(o, x):
                        E(f, f"{', '.join(changed)} changed in an {o.status} ADR; supersede instead (L5)")
                    if o.status in ("Superseded", "Rejected", "Deprecated") and x.status != o.status:
                        E(f, f"status changed from terminal {o.status} to {x.status} (L5)")
    idx = d / "README.md"
    if not idx.exists():
        E("README.md", "index missing; run 'adr.py index' (L10)")
    elif idx.read_text(encoding="utf-8") != index_text(d):
        E("README.md", "index out of date; run 'adr.py index' (L10)")
    for line in errs + warns:
        print(line)
    print(f"{len(adrs)} ADRs, {len(errs)} errors, {len(warns)} warnings")
    sys.exit(1 if errs or (a.strict and warns) else 0)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir")
    ap.add_argument("--owner", default=os.environ.get("ADR_OWNER", "agent"))
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("init")
    p = sp.add_parser("new"); p.add_argument("--title", required=True)
    p.add_argument("--template", choices=("nygard", "aws"), default="nygard")
    p.add_argument("--supersedes", nargs="*", type=int)
    p.add_argument("--amends", nargs="*", type=int)
    p.add_argument("--placeholder", action="store_true")
    p = sp.add_parser("renumber"); p.add_argument("file")
    p = sp.add_parser("accept"); p.add_argument("number", type=int)
    p.add_argument("--stakeholders"); p.add_argument("--version", default="1.0")
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("reject"); p.add_argument("number", type=int); p.add_argument("--reason", required=True)
    p = sp.add_parser("deprecate"); p.add_argument("number", type=int); p.add_argument("--reason", required=True)
    p = sp.add_parser("list"); p.add_argument("--status", choices=STATUSES); p.add_argument("--json", action="store_true")
    sp.add_parser("index")
    p = sp.add_parser("lint"); p.add_argument("--base"); p.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    d = find_dir(a.dir)
    globals()["cmd_" + a.cmd](a, d)


if __name__ == "__main__":
    main()
