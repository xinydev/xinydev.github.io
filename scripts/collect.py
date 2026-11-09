#!/usr/bin/env python3
"""Combine GitHub commit search with verified local history into data.json and data.js.

Search only ever adds: once a searched repo's commit is published in data.json it stays, even if a later
search misses it (delete the entry from data.json by hand to drop it). history.json is authoritative for its repos.

Usage: python3 scripts/collect.py   (needs the GitHub CLI `gh`, authenticated: gh auth login, or GH_TOKEN set)
"""
import json, subprocess, time, datetime, collections, sys, os
from pathlib import Path

USER = "xinydev"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data.json")
OUT_JS = os.path.join(ROOT, "data.js")
HISTORY = os.path.join(ROOT, "scripts", "history.json")

# canonical upstream repos; (owner/repo, era, project label, domain)
REPOS = [
    ("jemalloc/jemalloc",        "memory",  "jemalloc",             "C"),
    ("pytorch/pytorch",          "memory",  "PyTorch",              "C++/Python"),
    ("spdk/spdk-csi",            "storage", "SPDK-CSI",             "Go"),
    ("spdk/spdk",                "storage", "SPDK",                 "C"),
    ("torvalds/linux",           "storage", "Linux kernel",         "C"),
    ("xinydev/telemetry-solution","storage","Arm Telemetry Solution","Python"),
    ("kubernetes/kubernetes",    "cloud",   "Kubernetes",           "Go"),
    ("kubernetes/kubeadm",       "cloud",   "kubeadm",              "Go"),
    ("kubernetes/website",       "cloud",   "Kubernetes docs",      "Markdown"),
    ("kubernetes/test-infra",    "cloud",   "Kubernetes test-infra","Go"),
    ("kubernetes-sigs/kubetest2","cloud",   "kubetest2",            "Go"),
    ("kubernetes-sigs/kind",     "cloud",   "kind",                 "Go"),
    ("knative/eventing",         "cloud",   "Knative Eventing",     "Go"),
]
# repos whose canonical home is not on GitHub: link commits to the mirror but label the upstream
UPSTREAM_HOME = {
    "xinydev/telemetry-solution": "https://gitlab.arm.com/telemetry-solution/telemetry-solution",
}

def gh(args):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if r.returncode != 0:
        print("gh error:", r.stderr[:200], file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None

def commits_for(repo):
    for attempt in range(3):
        d = gh(["search", "commits", "--author", USER, "--repo", repo, "--limit", "300",
                "--json", "sha,commit,url,repository"])
        if d is not None:
            break
        time.sleep(30)
    else:
        return None
    out, seen = [], set()
    for c in d:
        if c["sha"] in seen:
            continue
        seen.add(c["sha"])
        out.append({
            "sha": c["sha"][:10],
            "date": c["commit"]["author"]["date"][:10],
            "title": c["commit"]["message"].splitlines()[0],
            "url": c["url"],
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out

def merge_commits(*groups, previous=()):
    # Commit URLs contain the full SHA; displayed SHAs are abbreviated.
    commits = {c["url"]: c for group in groups for c in group}
    # Preserve the published order when a search returns same-day commits in a different order.
    ordered = {c["url"]: c for c in previous if c["url"] in commits}
    ordered.update(commits)
    return sorted(ordered.values(), key=lambda c: c["date"], reverse=True)

def main():
    with open(HISTORY) as f:
        history = {p["repo"]: p for p in json.load(f)}
    try:
        with open(OUT, encoding="utf-8") as f:
            saved = json.load(f)
        cached_by_repo = {p["repo"]: p.get("commits", []) for p in saved["projects"]}
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        saved = {}
        cached_by_repo = {}
    live_repos = {repo for repo, _, _, _ in REPOS}
    specs = {repo: (era, label, lang) for repo, era, label, lang in REPOS}
    for repo, p in history.items():
        specs.setdefault(repo, (p["era"], p["label"], p["lang"]))
    projects = []
    for repo, (era, label, lang) in specs.items():
        cached = cached_by_repo.get(repo, [])
        kept, found = [], []
        if repo in live_repos:
            # GitHub search can return a partial result without reporting an error, so a miss
            # never removes a commit that is already published.
            kept = cached
            found = commits_for(repo)
            if found is None:                # the API failed: nothing new this run
                found = []
                print("search failed; kept published commits for", repo, file=sys.stderr)
        # Later groups win for the same commit: history.json pins attribution over search results.
        cs = merge_commits(kept, found, history.get(repo, {}).get("commits", []), previous=cached)
        projects.append({"repo": repo, "label": label, "era": era, "lang": lang,
                         "upstream": UPSTREAM_HOME.get(repo, f"https://github.com/{repo}"),
                         "count": len(cs), "first": cs[-1]["date"] if cs else None,
                         "last": cs[0]["date"] if cs else None, "commits": cs})
        if repo in live_repos:
            time.sleep(3)
    allc = [dict(c, repo=p["repo"], label=p["label"], era=p["era"]) for p in projects for c in p["commits"]]
    years = collections.Counter(c["date"][:4] for c in allc)
    data = {
        "user": USER,
        "name": "Xin Yang",
        "generated": saved.get("generated"),
        "totals": {"commits": len(allc), "projects": sum(1 for p in projects if p["count"]),
                   "years": [[year, years[year]] for year in sorted(years)],
                   "first": min(c["date"] for c in allc) if allc else None},
        "projects": projects,
    }
    if data != saved or not data["generated"]:
        data["generated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        saved = data

    script = "window.CONTRIB = " + json.dumps(saved, ensure_ascii=False) + ";\n"
    js_file = Path(OUT_JS)
    if not js_file.exists() or js_file.read_text(encoding="utf-8") != script:
        js_file.write_text(script, encoding="utf-8")
    print(f"collected {len(allc)} commits across {data['totals']['projects']} projects")

if __name__ == "__main__":
    main()
