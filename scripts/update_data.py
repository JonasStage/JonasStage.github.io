#!/usr/bin/env python3
"""Refresh data.json with GitHub, OpenAlex and (best-effort) Google Scholar numbers.

Runs inside the weekly GitHub Action, but can also be run locally:
    python scripts/update_data.py
"""
import datetime, json, os, re, sys, urllib.parse, urllib.request

GITHUB_USER      = os.environ.get("GITHUB_USER", "JonasStage")
SCHOLAR_ID       = os.environ.get("SCHOLAR_ID", "-6tGaCoAAAAJ")
OPENALEX_ID      = os.environ.get("OPENALEX_AUTHOR_ID", "A5041854845").strip()   # e.g. A5012345678
AUTHOR_NAME      = os.environ.get("AUTHOR_NAME", "Jonas Stage Sø")
CONTACT_EMAIL    = os.environ.get("CONTACT_EMAIL", "Jonassoe@biology.sdu.dk")  # OpenAlex "polite pool"
OUT              = os.path.join(os.path.dirname(__file__), "..", "data.json")


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "personal-site-updater", **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def norm(t):
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


# ───────────── GitHub ─────────────
def fetch_github():
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    repos, page = [], 1
    while True:
        chunk = get_json(f"https://api.github.com/users/{GITHUB_USER}/repos?per_page=100&type=owner&page={page}", headers)
        repos += chunk
        if len(chunk) < 100:
            break
        page += 1
    own = [r for r in repos if not r["fork"] and not r["archived"]]
    return {
        "count": len(own),
        "repos": {r["name"]: {"stars": r["stargazers_count"]} for r in own},
    }


# ───────────── OpenAlex ─────────────
def resolve_openalex_id():
    if OPENALEX_ID:
        return OPENALEX_ID
    q = urllib.parse.quote(AUTHOR_NAME)
    res = get_json(f"https://api.openalex.org/authors?search={q}&mailto={CONTACT_EMAIL}")["results"]
    print("OpenAlex author candidates (pin one with OPENALEX_AUTHOR_ID):")
    for a in res[:5]:
        inst = (a.get("last_known_institutions") or [{}])[0].get("display_name")
        print(f"  {a['id'].split('/')[-1]}  {a['display_name']}  works={a['works_count']}  {inst}")
    for a in res:
        insts = " ".join(i.get("display_name", "") for i in a.get("last_known_institutions") or [])
        if "Southern Denmark" in insts:
            return a["id"].split("/")[-1]
    raise RuntimeError("Could not identify OpenAlex author; set OPENALEX_AUTHOR_ID")


def short_name(full):
    parts = full.replace(".", " ").split()
    if len(parts) < 2:
        return full
    return "".join(p[0] for p in parts[:-1]).upper() + " " + parts[-1]


def fetch_openalex():
    aid = resolve_openalex_id()
    works, cursor = [], "*"
    while cursor:
        url = (f"https://api.openalex.org/works?filter=author.id:{aid}&per-page=100&cursor={cursor}"
               f"&mailto={CONTACT_EMAIL}")
        res = get_json(url)
        works += res["results"]
        cursor = res["meta"].get("next_cursor")
        if not res["results"]:
            break

    pubs, seen = [], set()
    for w in works:
        if w.get("type") not in ("article", "review"):
            continue                                   # skip preprints, datasets, errata …
        key = norm(w.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
        bib = w.get("biblio") or {}
        venue = src
        if bib.get("volume"):
            venue += f" {bib['volume']}"
            if bib.get("issue"):
                venue += f" ({bib['issue']})"
        names = [short_name(a["author"]["display_name"]) for a in w.get("authorships", [])]
        authors = ", ".join(names[:7]) + (" et al." if len(names) > 7 else "")
        pubs.append({
            "title": w["title"], "year": w.get("publication_year"), "venue": venue.strip(),
            "authors": authors, "citations": w.get("cited_by_count", 0), "doi": w.get("doi"),
        })
    pubs.sort(key=lambda p: (-(p["year"] or 0), -p["citations"]))
    return pubs


# ───────────── Google Scholar (best effort – Google often blocks cloud IPs) ─────────────
def fetch_scholar():
    try:
        from scholarly import scholarly
        a = scholarly.fill(scholarly.search_author_id(SCHOLAR_ID), sections=["indices", "publications"])
        return {
            "citations": a["citedby"],
            "publications": len(a["publications"]),
            "per_paper": {norm(p["bib"]["title"]): p.get("num_citations", 0) for p in a["publications"]},
        }
    except Exception as e:
        print("Google Scholar unavailable, falling back to OpenAlex:", repr(e)[:200])
        return None


def main():
    gh = fetch_github()
    pubs = fetch_openalex()
    sc = fetch_scholar()

    if sc:
        for p in pubs:
            p["citations"] = max(p["citations"], sc["per_paper"].get(norm(p["title"]), 0))
        citations, n_pubs, source = sc["citations"], max(sc["publications"], len(pubs)), "Google Scholar + OpenAlex"
    else:
        citations, n_pubs, source = sum(p["citations"] for p in pubs), len(pubs), "OpenAlex"

    data = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "stats": {"publications": n_pubs, "repositories": gh["count"], "citations": citations},
        "repos": gh["repos"],
        "publications": pubs,
    }

    # Only touch the file when something actually changed (avoids empty weekly commits)
    try:
        old = json.load(open(OUT, encoding="utf-8"))
        if {k: v for k, v in old.items() if k != "updated"} == {k: v for k, v in data.items() if k != "updated"}:
            print("No changes.")
            return
    except FileNotFoundError:
        pass
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Updated: {n_pubs} publications, {gh['count']} repos, {citations} citations ({source})")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
