#!/usr/bin/env python3
"""Refresh data.json with GitHub, OpenAlex and (best-effort) Google Scholar numbers.

Runs inside the weekly GitHub Action, but can also be run locally:
    python scripts/update_data.py
"""
import datetime, json, os, re, sys, urllib.parse, urllib.request

GITHUB_USER      = os.environ.get("GITHUB_USER", "JonasStage")
SCHOLAR_ID       = os.environ.get("SCHOLAR_ID", "IP8yMtkAAAAJ")
OPENALEX_ID      = os.environ.get("OPENALEX_AUTHOR_ID", "").strip()   # e.g. A5012345678
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


# ───────────── Google Scholar (source of truth for citations) ─────────────
def scholar_via_serpapi(key):
    """Reliable route: SerpApi's Google Scholar Author API (free tier: 100 searches/month)."""
    per_paper, total, start = {}, None, 0
    while True:
        url = ("https://serpapi.com/search.json?engine=google_scholar_author"
               f"&author_id={SCHOLAR_ID}&hl=en&num=100&start={start}&api_key={key}")
        res = get_json(url)
        if "error" in res:
            raise RuntimeError(res["error"])
        if total is None:
            table = (res.get("cited_by") or {}).get("table") or []
            total = next(row["citations"]["all"] for row in table if "citations" in row)
        arts = res.get("articles", [])
        for a in arts:
            per_paper[norm(a["title"])] = (a.get("cited_by") or {}).get("value") or 0
        if len(arts) < 100:
            break
        start += 100
    return {"citations": total, "publications": len(per_paper), "per_paper": per_paper}


def scholar_via_scholarly():
    """Free route: scrapes Scholar directly. Google often blocks GitHub's IP ranges."""
    from scholarly import scholarly
    a = scholarly.fill(scholarly.search_author_id(SCHOLAR_ID), sections=["indices", "publications"])
    return {
        "citations": a["citedby"],
        "publications": len(a["publications"]),
        "per_paper": {norm(p["bib"]["title"]): p.get("num_citations", 0) for p in a["publications"]},
    }


def fetch_scholar():
    attempts = []
    if os.environ.get("SERPAPI_KEY"):
        attempts.append(("SerpApi", lambda: scholar_via_serpapi(os.environ["SERPAPI_KEY"])))
    attempts.append(("scholarly", scholar_via_scholarly))
    for name, fn in attempts:
        try:
            result = fn()
            if result["citations"] is not None:
                print(f"Google Scholar OK via {name}")
                return result
        except Exception as e:
            print(f"Google Scholar via {name} failed: {repr(e)[:200]}")
    return None


def load_old():
    try:
        return json.load(open(OUT, encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def main():
    gh = fetch_github()
    pubs = fetch_openalex()
    sc = fetch_scholar()
    old = load_old()

    if sc:
        # Scholar numbers win everywhere; OpenAlex only supplies the metadata.
        for p in pubs:
            p["citations"] = sc["per_paper"].get(norm(p["title"]), 0)
        citations, n_pubs = sc["citations"], max(sc["publications"], len(pubs))
        source = "Google Scholar (citations) + OpenAlex (metadata)"
    elif old.get("citation_source") == "scholar":
        # Scholar blocked us: keep the last known Scholar numbers rather than
        # replacing them with (lower) OpenAlex ones.
        print("WARNING: Scholar unreachable - keeping previous Scholar citation numbers.")
        prev = {norm(x["title"]): x["citations"] for x in old.get("publications", [])}
        for p in pubs:
            p["citations"] = prev.get(norm(p["title"]), 0)
        citations = old["stats"]["citations"]
        n_pubs = max(old["stats"].get("publications", 0), len(pubs))
        source = old.get("source", "Google Scholar (cached)")
    else:
        print("WARNING: Scholar unreachable and no cached Scholar data - using OpenAlex numbers.")
        citations, n_pubs, source = sum(p["citations"] for p in pubs), len(pubs), "OpenAlex (Scholar unavailable)"

    data = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "citation_source": "scholar" if (sc or old.get("citation_source") == "scholar") else "openalex",
        "stats": {"publications": n_pubs, "repositories": gh["count"], "citations": citations},
        "repos": gh["repos"],
        "publications": pubs,
    }

    # Only touch the file when something actually changed (avoids empty weekly commits)
    if old and {k: v for k, v in old.items() if k != "updated"} == {k: v for k, v in data.items() if k != "updated"}:
        print("No changes.")
        return
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Updated: {n_pubs} publications, {gh['count']} repos, {citations} citations ({source})")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
