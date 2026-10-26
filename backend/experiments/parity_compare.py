"""
parity_compare.py
Compares two parity_check.py outputs (the PC's and the server's, same commit, same snapshot)
section by section and prints PASS / FAIL per check with the differences. Exit code 1 on any
FAIL. Only the JSON files are read: no database, no network.

    python experiments/parity_compare.py PC.json SERVER.json [--max-rows 20]

A section present in one file and not the other FAILs; a section in neither is not compared.
Within a file, any replay mismatch or last-digit difference FAILs (the plan: "any last-digit
difference is written down and decided on, never ignored"), as does any network attempt or
denied outcome-column read. Installed packages are checked against requirements-lock.txt on
each machine (extras are INFO, never compared between machines); the Python version must match
exactly; platform fields are INFO.
"""

import argparse
import json
import sys

SECTIONS = ("integrity", "models", "replay_paper", "replay_challenger", "end_to_end")


class Report:
    def __init__(self, max_rows: int = 20):
        self.checks: list[tuple[str, str, list[str]]] = []
        self.max_rows = max_rows

    def add(self, ok: bool, name: str, details=()) -> None:
        self.checks.append(("PASS" if ok else "FAIL", name, list(details)))

    def info(self, name: str, details=()) -> None:
        """Reported, never a failure."""
        self.checks.append(("INFO", name, list(details)))

    @property
    def failed(self) -> int:
        return sum(1 for s, _, _ in self.checks if s == "FAIL")

    def cap(self, items: list) -> list[str]:
        items = [str(x) for x in items]
        if len(items) > self.max_rows:
            return items[:self.max_rows] + [f"... and {len(items) - self.max_rows} more"]
        return items


def _num(v):
    """{"float": hex} -> 'repr (hex)' for display; anything else as is."""
    if isinstance(v, dict) and "float" in v:
        return f"{float.fromhex(v['float'])!r} ({v['float']})"
    if isinstance(v, str) and v.startswith(("0x", "-0x")):
        return f"{float.fromhex(v)!r} ({v})"
    return repr(v)


def _dict_diff(a: dict, b: dict, la: str, lb: str) -> list[str]:
    out = []
    for k in sorted(set(a) | set(b)):
        if k not in a:
            out.append(f"{k}: only in {lb} ({b[k]!r})")
        elif k not in b:
            out.append(f"{k}: only in {la} ({a[k]!r})")
        elif a[k] != b[k]:
            out.append(f"{k}: {la} {a[k]!r} vs {lb} {b[k]!r}")
    return out


def compare_meta(r: Report, a: dict, b: dict, la: str, lb: str) -> None:
    ma, mb = a.get("meta", {}), b.get("meta", {})
    r.add(ma.get("tool_version") == mb.get("tool_version") and ma.get("encoding") == mb.get("encoding"),
          "meta: same parity_check version and encoding",
          [f"{la} {ma.get('tool_version')}/{ma.get('encoding')} vs {lb} {mb.get('tool_version')}/{mb.get('encoding')}"])
    r.add(ma.get("git_commit") == mb.get("git_commit"), "meta: same git commit",
          [f"{la} {ma.get('git_commit')} vs {lb} {mb.get('git_commit')}"])
    r.add(not ma.get("git_dirty") and not mb.get("git_dirty"), "meta: no uncommitted changes to tracked files",
          [f"{la} dirty={ma.get('git_dirty')}, {lb} dirty={mb.get('git_dirty')}"])
    r.add(bool(ma.get("python_version")) and ma.get("python_version") == mb.get("python_version"),
          "meta: same Python version", [f"{la} {ma.get('python_version')} vs {lb} {mb.get('python_version')}"])
    # Installed packages are checked against the lock file on each machine, never against
    # each other (Windows and Linux legitimately differ in platform-only extras).
    for lab, m in ((la, ma), (lb, mb)):
        p = m.get("packages_vs_lock")
        if p is None:
            r.add(False, f"packages: every lock line installed at its version ({lab})", ["no packages_vs_lock in file"])
            continue
        r.add(p["pass"], f"packages: every {p['lock_file']} line installed at its version ({lab}: {p['pins']} pins)",
              r.cap([f"missing {x['name']}=={x['lock']}" for x in p["missing"]]
                    + [f"{x['name']}: lock {x['lock']}, installed {x['installed']}" for x in p["wrong_version"]]
                    + [f"unparsed {x}" for x in p["unparsed_lines"]]))
        r.info(f"packages: not in the lock ({lab}: {len(p['extra'])})",
               [f"{x['name']}=={x['version']}{' (' + x['note'] + ')' if x.get('note') else ''}" for x in p["extra"]])
    for lab, m in ((la, ma), (lb, mb)):
        r.info(f"platform ({lab})", [f"host {m.get('host')}", f"platform {m.get('platform')} {m.get('machine')}",
                                    f"UTC offset {m.get('local_utc_offset')}", f"thread env {m.get('thread_env')}",
                                    f"threadpools {m.get('threadpools')}"])


def compare_guards(r: Report, a: dict, b: dict, la: str, lb: str) -> None:
    r.add(a.get("exclusions") == b.get("exclusions"), "exclusions: same outcome columns excluded per table",
          r.cap(_dict_diff(a.get("exclusions", {}), b.get("exclusions", {}), la, lb)))
    for lab, x in ((la, a), (lb, b)):
        att = x.get("network", {}).get("attempts", [])
        r.add(not att, f"network: no connection attempted ({lab})", r.cap(att))
        den = x.get("denied_reads", [])
        r.add(not den, f"outcome columns: no read denied ({lab})", r.cap(sorted(set(den))))


def compare_integrity(r: Report, a: dict, b: dict, la: str, lb: str) -> None:
    for lab, x in ((la, a), (lb, b)):
        r.add(x.get("integrity_check") == ["ok"], f"integrity: PRAGMA integrity_check ok ({lab})",
              r.cap(x.get("integrity_check", ["missing"])))
        if "error" in x:
            r.add(False, f"integrity: ran ({lab})", [x["error"]])
    for k in ("journal_mode", "encoding", "page_size", "user_version", "schema_sha256"):
        r.add(a.get(k) == b.get(k), f"integrity: same {k}", [f"{la} {a.get(k)!r} vs {lb} {b.get(k)!r}"])
    ta, tb = a.get("tables", {}), b.get("tables", {})
    r.add(set(ta) == set(tb), "integrity: same tables",
          [f"only {la}: {sorted(set(ta) - set(tb))}", f"only {lb}: {sorted(set(tb) - set(ta))}"])
    for t in sorted(set(ta) & set(tb)):
        x, y = ta[t], tb[t]
        d = []
        if x["hashed_columns"] != y["hashed_columns"]:
            d.append(f"hashed columns differ: {la} {x['hashed_columns']} vs {lb} {y['hashed_columns']}")
        if x["rows"] != y["rows"]:
            d.append(f"rows: {la} {x['rows']:,} vs {lb} {y['rows']:,}")
        if x["sha256"] != y["sha256"]:
            d.append(f"sha256: {la} {x['sha256']} vs {lb} {y['sha256']}")
        r.add(not d, f"integrity: table {t} ({x['rows']:,} rows, {len(x['hashed_columns'])} columns)", d)


def compare_models(r: Report, a: dict, b: dict, la: str, lb: str) -> None:
    fa, fb = a.get("files", {}), b.get("files", {})
    for f in sorted(set(fa) | set(fb)):
        if f not in fa or f not in fb:
            r.add(False, f"models: {f}", [f"only in {lb if f not in fa else la}"])
            continue
        x, y = fa[f], fb[f]
        d = [f"{lab}: pre-registration {z['status']}" for lab, z in ((la, x), (lb, y)) if z["status"] == "MISMATCH"]
        d += [f"{lab}: fingerprint {z['fingerprint_status']}" for lab, z in ((la, x), (lb, y))
              if z.get("fingerprint_status") == "MISMATCH"]
        if x["sha256"] != y["sha256"]:
            d.append(f"sha256: {la} {x['sha256']} vs {lb} {y['sha256']}")
        if x.get("fingerprint") != y.get("fingerprint"):
            d.append(f"fingerprint: {la} {x.get('fingerprint')} vs {lb} {y.get('fingerprint')}")
        note = "" if x["status"] == y["status"] == "OK" else f" [{x['status']} / {y['status']}]"
        r.add(not d, f"models: {f}{note}", d)
    for lab, x in ((la, a), (lb, b)):
        pins = x.get("code_pins", {})
        bad = [f for f, v in pins.items() if not v.get("matches_prereg")]
        r.add(bool(pins) and not bad and x.get("code_fingerprint_pin_matches_prereg", False),
              f"models: hashes pinned in code match the pre-registration ({lab})", bad)


def compare_replay(r: Report, a: dict, b: dict, la: str, lb: str, name: str) -> None:
    for lab, x in ((la, a), (lb, b)):
        if "error" in x:
            r.add(False, f"{name}: ran ({lab})", [x["error"]])
            continue
        for f, v in x.get("fields", {}).items():
            r.add(v["mismatch"] == 0, f"{name}: {f} matches the stored value ({lab}: {v['checked']} checked)",
                  r.cap([f"id {m['id']} {m.get('player_name')} {m.get('stat_type')}: stored {_num(m['stored'])}, "
                         f"recomputed {_num(m['recomputed'])}{' (' + m['note'] + ')' if m.get('note') else ''}"
                         for m in v["mismatch_rows"]]))
            r.add(v["last_digit"] == 0, f"{name}: {f} has no last-digit difference ({lab})",
                  [f"{v['last_digit']} rows, max abs {v['last_digit_max_abs']!r}, max {v['last_digit_max_ulps']} ulp"]
                  + r.cap([f"id {m['id']}: stored {_num(m['stored'])}, recomputed {_num(m['recomputed'])}"
                           for m in v["last_digit_rows"]]))
    r.add(a.get("skipped_by_status") == b.get("skipped_by_status"), f"{name}: same rows skipped by status",
          _dict_diff(a.get("skipped_by_status", {}), b.get("skipped_by_status", {}), la, lb))
    ra, rb = a.get("recomputed", {}), b.get("recomputed", {})
    d = [f"id {i}: only in {lb if i not in ra else la}" for i in sorted(set(ra) ^ set(rb), key=int)]
    for i in sorted(set(ra) & set(rb), key=int):
        for f in sorted(set(ra[i]) | set(rb[i])):
            if ra[i].get(f) != rb[i].get(f):
                d.append(f"id {i} {f}: {la} {_num(ra[i].get(f))} vs {lb} {_num(rb[i].get(f))}")
    r.add(not d and bool(ra), f"{name}: recomputed values bitwise identical across machines ({len(ra)} rows)",
          r.cap(d) if ra else ["no rows re-scored"])


def compare_end_to_end(r: Report, a: dict, b: dict, la: str, lb: str) -> None:
    for lab, x in ((la, a), (lb, b)):
        errs = x.get("errors", []) + ([x["error"]] if "error" in x else []) + \
            [f"refused connection {c}" for c in x.get("refused_connections", [])]
        r.add(not errs, f"end-to-end: ran without errors ({lab})", r.cap(errs))
        r.add(x.get("temp_dir_removed", False), f"end-to-end: temp copy removed ({lab})", [x.get("temp_dir", "")])
    r.add(a.get("date") == b.get("date"), "end-to-end: same date", [f"{la} {a.get('date')} vs {lb} {b.get('date')}"])

    def pulls(x):
        return [(p["pull_id"], p["game_id"], p["captured_at"]) for p in x.get("pulls", [])]
    r.add(pulls(a) == pulls(b), "end-to-end: same pulls and captures", [f"{la} {pulls(a)}", f"{lb} {pulls(b)}"])
    ea, eb = a.get("export", {}), b.get("export", {})
    for t in sorted(set(ea) | set(eb)):
        if t not in ea or t not in eb:
            r.add(False, f"end-to-end: {t} export", [f"only in {lb if t not in ea else la}"])
            continue
        x, y = ea[t], eb[t]
        if x["sha256"] == y["sha256"] and x["columns"] == y["columns"]:
            r.add(True, f"end-to-end: {t} export identical ({x['n']} rows, {len(x['columns'])} columns)")
            continue
        d = []
        if x["columns"] != y["columns"]:
            d.append(f"columns: {la} {x['columns']} vs {lb} {y['columns']}")
        d.append(f"rows: {la} {x['n']} vs {lb} {y['n']}")
        if x["columns"] == y["columns"]:
            for i, (ra, rb) in enumerate(zip(x["rows"], y["rows"])):
                for c, va, vb in zip(x["columns"], ra, rb):
                    if va != vb:
                        d.append(f"row {i} ({ra[x['columns'].index('player_name')]} "
                                 f"{ra[x['columns'].index('stat_type')]}) {c}: {la} {_num(va)} vs {lb} {_num(vb)}")
        r.add(False, f"end-to-end: {t} export identical", r.cap(d))


def compare(a: dict, b: dict, la: str = "PC", lb: str = "server", max_rows: int = 20) -> Report:
    r = Report(max_rows)
    compare_meta(r, a, b, la, lb)
    compare_guards(r, a, b, la, lb)
    for s in SECTIONS:
        if s not in a and s not in b:
            continue
        if s not in a or s not in b:
            r.add(False, f"{s}: present in both files", [f"only in {lb if s not in a else la}"])
            continue
        if s == "integrity":
            compare_integrity(r, a[s], b[s], la, lb)
        elif s == "models":
            compare_models(r, a[s], b[s], la, lb)
        elif s in ("replay_paper", "replay_challenger"):
            compare_replay(r, a[s], b[s], la, lb, s)
        else:
            compare_end_to_end(r, a[s], b[s], la, lb)
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare two parity_check.py JSON outputs.")
    ap.add_argument("first", help="the PC's JSON")
    ap.add_argument("second", help="the server's JSON")
    ap.add_argument("--max-rows", type=int, default=20, help="differences listed per check")
    args = ap.parse_args(argv)
    with open(args.first, encoding="utf-8") as f:
        a = json.load(f)
    with open(args.second, encoding="utf-8") as f:
        b = json.load(f)
    la = f"first ({a.get('meta', {}).get('host', '?')})"
    lb = f"second ({b.get('meta', {}).get('host', '?')})"
    r = compare(a, b, la, lb, args.max_rows)
    for status, name, details in r.checks:
        print(f"{status}  {name}")
        if status != "PASS":
            for d in details:
                print(f"        {d}")
    n_info = sum(1 for s, _, _ in r.checks if s == "INFO")
    print(f"\n{len(r.checks) - r.failed - n_info} PASS, {r.failed} FAIL, {n_info} INFO")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
