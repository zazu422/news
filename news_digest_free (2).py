#!/usr/bin/env python3
"""
FREE news digest. No API key, no installs, no AI.

It reads the public news feeds (RSS) of Bangladeshi and international outlets,
groups together stories reported by several outlets (a good sign of a
reach-worthy story), ranks them, and saves a clean web page you can open.

Run:
    python news_digest_free.py              (Windows: py news_digest_free.py)
    python news_digest_free.py --lang bn    (Bangla outlets)
    python news_digest_free.py --count 12 --hours 12

To add or remove an outlet, edit the FEEDS list below.
"""
import argparse, html, json, os, re, time, webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

# (outlet name, feed address, section)  section: BD = Bangladesh, WORLD = International
FEEDS = {
    "en": [
        ("The Daily Star", "https://www.thedailystar.net/frontpage/rss.xml", "BD"),
        ("Prothom Alo English", "https://en.prothomalo.com/feed", "BD"),
        ("Dhaka Tribune", "https://www.dhakatribune.com/feed", "BD"),
        ("The Business Standard", "https://www.tbsnews.net/rss.xml", "BD"),
        ("Google News Bangladesh", "https://news.google.com/rss?hl=en-BD&gl=BD&ceid=BD:en", "BD"),
        ("Google News: Bangladesh search", "https://news.google.com/rss/search?q=Bangladesh+when:1d&hl=en-BD&gl=BD&ceid=BD:en", "BD"),
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "WORLD"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "WORLD"),
        ("The Guardian World", "https://www.theguardian.com/world/rss", "WORLD"),
        ("DW", "https://rss.dw.com/rdf/rss-en-all", "WORLD"),
        ("NPR World", "https://feeds.npr.org/1004/rss.xml", "WORLD"),
        ("Google News World", "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-BD&gl=BD&ceid=BD:en", "WORLD"),
    ],
    "bn": [
        ("প্রথম আলো", "https://www.prothomalo.com/feed", "BD"),
        ("জাগো নিউজ", "https://www.jagonews24.com/rss/rss.xml", "BD"),
        ("বাংলা ট্রিবিউন", "https://www.banglatribune.com/feed", "BD"),
        ("বিডি২৪লাইভ", "https://www.bd24live.com/bangla/feed", "BD"),
        ("রাইজিংবিডি", "https://www.risingbd.com/rss/rss.xml", "BD"),
        ("Google News বাংলাদেশ", "https://news.google.com/rss?hl=bn&gl=BD&ceid=BD:bn", "BD"),
        ("BBC বাংলা", "https://feeds.bbci.co.uk/bengali/rss.xml", "WORLD"),
        ("DW বাংলা", "https://rss.dw.com/xml/rss-bn-all", "WORLD"),
        ("Google News বিশ্ব", "https://news.google.com/rss/headlines/section/topic/WORLD?hl=bn&gl=BD&ceid=BD:bn", "WORLD"),
    ],
}
SECTION_NAMES = {
    "en": {"BD": "Bangladesh", "WORLD": "International"},
    "bn": {"BD": "বাংলাদেশ", "WORLD": "আন্তর্জাতিক"},
}
DHAKA = timezone(timedelta(hours=6))
STOP = set("""the a an and or of to in on for with at by from as is are was were be been it its this that
these those after over into than then will says say said new more not but has have had his her their
about up out amid who what when where why how can could may might would just also""".split())


def local(tag):
    return tag.rsplit("}", 1)[-1]


def clean(text, limit=300):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        text = (cut[: cut.rfind(" ")] if " " in cut else cut).rstrip(",;:") + "..."
    return text


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(data, outlet, section):
    """Turn raw feed bytes into a list of story dictionaries."""
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        f = {}
        for child in el:
            name = local(child.tag)
            if name == "link":
                f.setdefault("link", child.get("href") or (child.text or "").strip())
            elif name in ("title", "description", "summary", "pubDate", "published", "updated", "date"):
                f.setdefault(name, child.text or "")
        title = clean(f.get("title"), 200)
        link = f.get("link", "")
        if not title or not link.startswith("http"):
            continue
        source = outlet
        if outlet.startswith("Google News") and " - " in title:  # Google adds "- Publisher"
            title, source = title.rsplit(" - ", 1)
        summary = clean(f.get("description") or f.get("summary"))
        if summary.lower().startswith(title.lower()[:40]) or len(summary) < 25:
            summary = ""  # skip empty or just-the-headline summaries
        when = parse_date(f.get("pubDate") or f.get("published") or f.get("updated") or f.get("date"))
        items.append(dict(title=title, link=link, source=source.strip(), summary=summary,
                          when=when, section=section))
    return items


def fetch(feed):
    outlet, url, section = feed
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (NewsDigest)"})
        with urlopen(req, timeout=20) as r:
            return outlet, parse_feed(r.read(), outlet, section), None
    except Exception as e:
        return outlet, [], str(e)[:80]


def keywords(title):
    words = re.split(r"[\s\-–—:;,.!?\"'“”‘’()\[\]|/]+", title.lower())
    return {w for w in words if len(w) > 2 and w not in STOP}


def cluster(items):
    """Group stories about the same event, whichever outlet reported them."""
    groups = []
    for it in items:
        kw = keywords(it["title"])
        for g in groups:
            common = len(kw & g["kw"])
            if common >= 3 and common / max(1, min(len(kw), len(g["kw"]))) >= 0.5:
                g["items"].append(it)
                break
        else:
            groups.append({"kw": kw, "items": [it]})
    return groups


def rank(groups):
    def key(g):
        outlets = {i["source"] for i in g["items"]}
        newest = max((i["when"] for i in g["items"] if i["when"]),
                     default=datetime(2000, 1, 1, tzinfo=timezone.utc))
        return (len(outlets), newest)
    return sorted(groups, key=key, reverse=True)


CSS = """
:root{--bg:#f3f6f4;--surface:#fff;--ink:#14201b;--muted:#5b6a63;--line:#dce4df;--green:#0a6b4f;--green-soft:#e1f0ea;--on-green:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#0e1512;--surface:#151e1a;--ink:#e9f0ec;--muted:#98a8a0;--line:#25322c;--green:#52cfa5;--green-soft:#173029;--on-green:#08241b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.6 'Hind Siliguri','Inter',system-ui,'Noto Sans Bengali',sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:0 20px 64px}
.mast{padding:44px 0 22px}
.brand{font:600 15px 'Hind Siliguri',sans-serif;color:var(--green);letter-spacing:.02em}
.date{font:600 clamp(28px,5vw,40px)/1.15 'Source Serif 4','Noto Serif Bengali',Georgia,serif;margin:6px 0 4px}
.sub{color:var(--muted);font-size:15px}
.switch{float:right;color:var(--green);font-weight:600;text-decoration:none;font-size:15px}
.tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5;padding-top:6px}
.tab{appearance:none;border:0;background:none;color:var(--muted);font:600 16px 'Hind Siliguri',sans-serif;padding:12px 14px;cursor:pointer;border-bottom:3px solid transparent;margin-bottom:-1px}
.tab[aria-selected=true]{color:var(--ink);border-bottom-color:var(--green)}
.tab span{color:var(--muted);font-weight:400;margin-left:6px}
.panel{padding-top:24px}
.lead{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:28px 28px 24px;margin-bottom:8px}
.lead h3{font-size:clamp(24px,4.2vw,32px);line-height:1.2;margin:10px 0 12px}
.lead p{font-size:18px}
h3{font-family:'Source Serif 4','Noto Serif Bengali',Georgia,serif;font-weight:600;margin:0}
.row{padding:22px 4px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row h3{font-size:20px;line-height:1.3;margin:6px 0 6px}
p{margin:0 0 14px;color:var(--muted)}
.lead p{color:var(--ink)}
.meta{display:flex;flex-wrap:wrap;align-items:center;gap:6px 16px;font-size:14px;color:var(--muted)}
.meta b{color:var(--ink);font-weight:600}
.dots{display:inline-flex;gap:3px;align-items:center}
.dots i{width:8px;height:8px;border-radius:50%;background:var(--line)}
.dots i.on{background:var(--green)}
.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.btn{font:600 15px 'Hind Siliguri',sans-serif;border-radius:8px;padding:8px 16px;cursor:pointer;text-decoration:none;display:inline-block;border:1px solid var(--green)}
.btn.primary{background:var(--green);color:var(--on-green)}
.btn.ghost{background:none;color:var(--green)}
.btn:hover{filter:brightness(1.08)}
.btn:focus-visible,.tab:focus-visible,a:focus-visible{outline:3px solid var(--green);outline-offset:2px}
.also{font-size:14px;color:var(--muted);margin-top:12px}
.also a{color:var(--green)}
.empty{color:var(--muted);padding:20px 0}

.note{color:var(--muted);font-size:13px;margin-top:36px}
@media(max-width:560px){.lead{padding:20px 18px}}
"""

JS = """
const tabs=[...document.querySelectorAll('.tab')],panels=[...document.querySelectorAll('.panel')];
function show(i){tabs.forEach((t,k)=>t.setAttribute('aria-selected',k===i));panels.forEach((p,k)=>p.hidden=k!==i)}
tabs.forEach((t,i)=>t.addEventListener('click',()=>show(i)));
const loaded=Date.now();document.addEventListener('visibilitychange',()=>{if(!document.hidden&&Date.now()-loaded>600000)location.reload()});if(tabs.length)show(0);
document.querySelectorAll('[data-copy]').forEach(b=>b.addEventListener('click',()=>{
 const text=b.dataset.copy,done=()=>{const o=b.textContent;b.textContent=b.dataset.done;setTimeout(()=>b.textContent=o,1500)};
 const fallback=()=>{const a=document.createElement('textarea');a.value=text;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove();done()};
 if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(text).then(done,fallback)}else fallback()}));
"""

LABELS = {
    "en": dict(read="Read full story", copy="Copy title + summary", copylink="Copy with link", copied="Copied", also="Also reported by",
               outlets="outlets", outlet="outlet", updated="Updated", window="last {h} hours",
               ai="AI summary", limited="Short note only, open the link for details",
               aiwarn="Titles and summaries are written by AI from each article. Check the original link before sharing.",
               none="No stories found. Check your internet connection and run the tool again."),
    "bn": dict(read="পুরো খবর পড়ুন", copy="শিরোনাম ও সারসংক্ষেপ কপি", copylink="লিংকসহ কপি", copied="কপি হয়েছে", also="আরও প্রকাশ করেছে",
               outlets="টি সংবাদমাধ্যম", outlet="টি সংবাদমাধ্যম", updated="আপডেট", window="গত {h} ঘণ্টা",
               ai="এআই সারসংক্ষেপ", limited="সংক্ষিপ্ত নোট, বিস্তারিত জানতে লিংকে যান",
               aiwarn="শিরোনাম ও সারসংক্ষেপ এআই লিখেছে। শেয়ার করার আগে মূল লিংক দেখে নিন।",
               none="কোনো খবর পাওয়া যায়নি। ইন্টারনেট সংযোগ দেখে আবার চালান।"),
}


# ---------- AI mode: read each article and write a new title + summary ----------
def has_ai():
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))


def article_text(url):
    """Download an article page and return its main paragraph text."""
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (NewsDigest)"})
        with urlopen(req, timeout=15) as r:
            raw = r.read(1_500_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""
    raw = re.sub(r"(?is)<(script|style|noscript|nav|footer|header|aside)[^>]*>.*?</\1>", " ", raw)
    paras = [clean(p, 100000) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", raw)]
    return " ".join(p for p in paras if len(p) > 60)[:5000]


def gather(group):
    """Pick the best outlet in the group that we can actually read."""
    cands = sorted(group["items"], key=lambda i: ("news.google.com" in i["link"], -len(i["summary"])))
    for it in cands[:3]:
        if "news.google.com" in it["link"]:
            continue  # Google News links hide the real article
        text = article_text(it["link"])
        if len(text) >= 400:
            group["lead"], group["text"] = it, text
            return
    group["lead"], group["text"] = cands[0], cands[0]["summary"]


def post_json(url, headers, body, timeout=180):
    req = Request(url, json.dumps(body).encode("utf-8"), {"content-type": "application/json", **headers})
    with urlopen(req, timeout=timeout) as r:
        return json.load(r)


def call_ai(prompt):
    claude_key, gem_key = os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("GEMINI_API_KEY")
    if claude_key:
        data = post_json("https://api.anthropic.com/v1/messages",
                         {"x-api-key": claude_key, "anthropic-version": "2023-06-01"},
                         {"model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"), "max_tokens": 6000,
                          "messages": [{"role": "user", "content": prompt}]})
        return "".join(b.get("text", "") for b in data["content"])
    models = [os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL") else \
        ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.1-flash-lite"]
    last = ""
    for model in models:
        for attempt in range(2):
            try:
                data = post_json(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    {"x-goog-api-key": gem_key},
                    {"contents": [{"parts": [{"text": prompt}]}],
                     "generationConfig": {"responseMimeType": "application/json"}})
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except HTTPError as err:
                last = f"{model}: HTTP {err.code}"
                if err.code == 429 and attempt == 0:
                    time.sleep(25)  # free plan speed limit, wait and retry once
                    continue
                break  # try the next model
    raise RuntimeError("AI service refused the request (" + last + ")")


def load_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(path, cache):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(list(cache.items())[-400:]), f, ensure_ascii=False)
    except Exception:
        pass


def ai_rewrite(groups, lang):
    name = "Bangla (বাংলা)" if lang == "bn" else "English"
    payload = [{"id": k, "outlet": g["lead"]["source"], "headline": g["lead"]["title"],
                "article_text": g["text"][:4500]} for k, g in enumerate(groups)]
    prompt = f"""You are a careful news editor. For each story below, write a new headline and a summary in {name}.
Rules:
- Use ONLY facts found in the given text. Never add facts, numbers, names or quotes that are not there.
- Headline: clear, neutral, under 90 characters, no clickbait.
- Summary: about 100 words (between 90 and 110), written as one clear paragraph: what happened, who is involved, when and where, key background from the text, and why it matters.
- If article_text is empty or very short, write ONE cautious sentence based only on the headline and set "limited" to true.
Reply with ONLY a JSON array, no other text: [{{"id": 0, "title": "...", "summary": "...", "limited": false}}]
Stories:
{json.dumps(payload, ensure_ascii=False)}"""
    match = re.search(r"\[.*\]", call_ai(prompt), re.S)
    for r in json.loads(match.group(0)):
        k = r.get("id")
        if isinstance(k, int) and 0 <= k < len(groups) and r.get("title"):
            groups[k]["ai"] = r


def ago(when, lang):
    if not when:
        return ""
    mins = int((datetime.now(timezone.utc) - when).total_seconds() // 60)
    if lang == "bn":
        if mins < 60:
            return f"{max(mins, 1)} মিনিট আগে"
        return f"{mins // 60} ঘণ্টা আগে" if mins < 1440 else f"{mins // 1440} দিন আগে"
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 1440:
        h = mins // 60
        return f"{h} hour{'s' if h > 1 else ''} ago"
    d = mins // 1440
    return f"{d} day{'s' if d > 1 else ''} ago"


def story_html(g, L, lang, lead=False):
    e = html.escape
    its = sorted(g["items"], key=lambda i: (len(i["summary"]) > 0, i["when"] is not None), reverse=True)
    top = g.get("lead") or its[0]
    ai = g.get("ai") or {}
    title = ai.get("title") or top["title"]
    body = ai.get("summary") or top["summary"]
    outlets = sorted({i["source"] for i in g["items"]})
    n = len(outlets)
    dots = "".join('<i class="on"></i>' if k < min(n, 5) else "<i></i>" for k in range(5))
    count = f'{n} {L["outlets"] if lang == "en" and n > 1 else L["outlet"]}' if lang == "en" else f'{n}{L["outlets"]}'
    copy_plain = "\n\n".join(x for x in (title, body) if x)
    copy_link = copy_plain + "\n\n" + top["link"]
    summary = f'<p>{e(body)}</p>' if body else ""
    tag = (f'<span>{L["limited"] if ai.get("limited") else L["ai"]}</span>') if ai else ""
    seen, links = set(), []
    for i in its:
        if i["source"] != top["source"] and i["source"] not in seen and len(links) < 4:
            seen.add(i["source"])
            links.append(f'<a href="{e(i["link"])}" target="_blank" rel="noopener">{e(i["source"])}</a>')
    also = f'<div class="also">{L["also"]}: {", ".join(links)}</div>' if links else ""
    cls = "lead" if lead else "row"
    return (f'<article class="{cls}"><div class="meta"><b>{e(top["source"])}</b>'
            f'<span>{e(ago(top["when"], lang))}</span>{tag}'
            f'<span class="dots" title="{e(count)}">{dots}<span style="margin-left:6px">{e(count)}</span></span></div>'
            f'<h3>{e(title)}</h3>{summary}'
            f'<div class="actions"><a class="btn primary" href="{e(top["link"])}" target="_blank" rel="noopener">{L["read"]}</a>'
            f'<button class="btn ghost" type="button" data-copy="{e(copy_plain, quote=True)}" data-done="{L["copied"]}">{L["copy"]}</button>'
            f'<button class="btn ghost" type="button" data-copy="{e(copy_link, quote=True)}" data-done="{L["copied"]}">{L["copylink"]}</button></div>'
            f'{also}</article>')


def render(sections, lang, hours, ai_used=False, switch=None, ai_note=""):
    e = html.escape
    L = LABELS[lang]
    now = datetime.now(DHAKA)
    tabs = "".join(f'<button class="tab" role="tab" type="button">{e(name)}<span>{len(groups)}</span></button>'
                   for name, groups in sections)
    panels = []
    for name, groups in sections:
        if not groups:
            panels.append(f'<section class="panel"><p class="empty">{L["none"]}</p></section>')
            continue
        body = story_html(groups[0], L, lang, lead=True) + "".join(story_html(g, L, lang) for g in groups[1:])
        panels.append(f'<section class="panel" aria-label="{e(name)}">{body}</section>')
    return f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>News Digest</title>
<link rel="manifest" href="manifest.json"><meta name="theme-color" content="#0a6b4f">
<link rel="apple-touch-icon" href="icon-192.png"><meta name="apple-mobile-web-app-capable" content="yes">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hind+Siliguri:wght@400;600&family=Noto+Serif+Bengali:wght@600&family=Source+Serif+4:wght@600&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body><div class="wrap">
<header class="mast">{f'<a class="switch" href="{e(switch)}">{"English" if lang == "bn" else "বাংলা"}</a>' if switch else ""}<div class="brand">News Digest</div>
<div class="date">{e(now.strftime("%A, %d %B %Y"))}</div>
<div class="sub">{L["updated"]} {e(now.strftime("%I:%M %p"))} Dhaka time, {L["window"].format(h=hours)}</div></header>
<nav class="tabs" role="tablist">{tabs}</nav>
{"".join(panels)}
{f'<p class="note">{L["aiwarn"]}</p>' if ai_used else ""}
{f'<p class="note"><b>AI summaries are off.</b> {e(ai_note)}</p>' if ai_note else ""}
<script>{JS}</script></div></body></html>"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=10, help="stories per section")
    p.add_argument("--hours", type=int, default=24, help="how far back to look")
    p.add_argument("--lang", choices=["en", "bn"], default="en")
    p.add_argument("--out", default="", help="output file name (default: dated name)")
    p.add_argument("--switch", default="", help="link to the other language page")
    p.add_argument("--cache", default="ai_cache.json", help="file that remembers finished AI summaries")
    p.add_argument("--no-ai", action="store_true", help="skip AI titles and summaries")
    p.add_argument("--no-open", action="store_true", help="do not open the page automatically")
    args = p.parse_args()

    print("Collecting news from the feeds, please wait...")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch, FEEDS[args.lang]))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    stories = []
    for outlet, items, err in results:
        if err:
            print(f"  FAILED  {outlet}: {err}")
        else:
            print(f"  ok      {outlet}: {len(items)} stories")
            stories += [i for i in items if i["when"] is None or i["when"] >= cutoff]

    sections = []
    for key in ("BD", "WORLD"):
        groups = rank(cluster([s for s in stories if s["section"] == key]))[: args.count]
        sections.append((SECTION_NAMES[args.lang][key], groups))

    ai_used = False
    ai_note = ""
    if not args.no_ai and not has_ai():
        ai_note = "No Google AI key was found. Add a secret named GEMINI_API_KEY in the repository settings, then run Update news again."
        print("\nAI mode is off because no key was found (see the setup steps). Showing plain headlines.")
    elif not args.no_ai:
        try:
            print("\nReading the articles...")
            allg = [g for _, gs in sections for g in gs]
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(gather, allg))
            cache = load_cache(args.cache)
            try:
                for name, gs in sections:
                    todo = []
                    for g in gs:
                        hit = cache.get(f"{args.lang}|v2|{g['lead']['link']}")
                        if hit:
                            g["ai"] = hit
                        else:
                            todo.append(g)
                    if todo:
                        print(f"Writing titles and summaries: {name} ({len(todo)} new) ...")
                        ai_rewrite(todo, args.lang)
                        for g in todo:
                            if g.get("ai"):
                                cache[f"{args.lang}|v2|{g['lead']['link']}"] = g["ai"]
                    elif gs:
                        print(f"{name}: nothing new, using saved summaries")
            finally:
                save_cache(args.cache, cache)
            ai_used = True
        except Exception as ex:
            ai_used = any(g.get("ai") for _, gs in sections for g in gs)
            ai_note = f"The AI step failed: {str(ex)[:150]}"
            print(f"\nAI step failed ({str(ex)[:120]}). Stories without a summary show plain headlines.")

    fname = args.out or f"digest_free_{datetime.now(DHAKA).strftime('%Y-%m-%d_%H%M')}.html"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(render(sections, args.lang, args.hours, ai_used, args.switch or None, ai_note))
    print(f"\nDone! Saved as {fname}")
    if not args.no_open:
        webbrowser.open(fname)


if __name__ == "__main__":
    main()
