#!/usr/bin/env python3
"""Scrape Northeastern Dialogue of Civilizations programs from the GEO portal into a CSV.

The GEO program portal (goglobal.northeastern.edu) is a JavaScript app, so this
script drives a headless Chromium browser over the same public pages a visitor
would open and records the JSON those pages load:

1. Program Discovery, filtered to "Dialogue of Civilizations", lists every program.
2. Each site filter (Colleges and Schools, Academic Subject, Program Status, ...)
   is applied on top of that, one value at a time, to learn which programs match it.
3. Each program's brochure page supplies dates, faculty, courses, and costs.

Raw responses are cached in --cache-dir, so the CSV can be rebuilt with
--from-cache without touching the network.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import re
import sys
import functools
import urllib.parse
from pathlib import Path

print = functools.partial(print, flush=True)  # show progress even when output is redirected

PORTAL = "https://goglobal.northeastern.edu/_portal"
DISCOVERY_URL = PORTAL + "/program-discovery?search="
BROCHURE_URL = PORTAL + "/tds-program-brochure?programid={}"
HONORS_URL = "https://honors.northeastern.edu/global-2/global-opportunities/dialogues-of-civilization/"
COURSE_GUIDE_URL = "https://geo.northeastern.edu/doc-courses/"

# NUPath codes used by the GEO DOC Course Guide.
NUPATHS = {
    "ND": "Engaging with the Natural and Designed World",
    "EI": "Exploring Creative Expression and Innovation",
    "IC": "Interpreting Culture",
    "FQ": "Conducting Formal and Quantitative Reasoning",
    "SI": "Understanding Societies and Institutions",
    "AD": "Analyzing and Using Data",
    "DD": "Engaging Difference and Diversity",
    "ER": "Employing Ethical Reasoning",
    "WF": "First-Year Writing",
    "WI": "Writing Intensive",
    "WD": "Advanced Writing in the Disciplines",
    "EX": "Integrating Knowledge and Skills Through Experience",
    "CE": "Capstone Experience",
}

DOC_FILTER = {
    "filterType": "programDiscoveryProgramParameters",
    "filterValues": [{"id": 10003, "value": "Dialogue of Civilizations"}],
}

# Site filters with a list of values (program parameter id -> CSV column).
LIST_FILTERS = {
    10034: "colleges_and_schools",
    10048: "college_endorsed",
    10051: "academic_subjects",
    10039: "program_status",
    10053: "institution_type",
}
# Site filters shown as on/off toggles: CSV column -> filter that the toggle adds.
TOGGLE_FILTERS = {
    "accepting_applications": {
        "filterType": "programDiscoveryAcceptingApplications",
        "filterValues": [{"value": True}],
    },
    "program_sponsored_housing": {
        "filterType": "programDiscoveryProgramParameters",
        "filterValues": [{"id": 10052, "value": "Yes"}],
    },
    "us_accredited_institution": {
        "filterType": "programDiscoveryProgramParameters",
        "filterValues": [{"id": 10054, "value": "Yes"}],
    },
}

PAGE_TIMEOUT_MS = 60_000
DEBUG_DIR = Path("debug")  # screenshots and HTML of pages that timed out, for diagnosing CI failures
BROCHURE_PARTS = ("program", "brochure", "dates")  # JSON a brochure page must load to be complete
PAGE_ATTEMPTS = 3  # tries per page before giving up, for slow or flaky portal responses


def discovery_url(filters: list[dict]) -> str:
    return DISCOVERY_URL + urllib.parse.quote(json.dumps(filters, separators=(",", ":")))


# --------------------------------------------------------------------------- scraping


def timeout_errors() -> tuple[type[BaseException], ...]:
    """Our own waits raise asyncio's TimeoutError; Playwright's page.goto and
    wait_for_load_state raise its own TimeoutError, which is not a subclass."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    return asyncio.TimeoutError, PlaywrightTimeoutError


async def run_search(context, sem, filters: list[dict], want_options: bool = False) -> dict:
    """Open Program Discovery with `filters` and scroll until the results stop loading."""
    for attempt in range(1, PAGE_ATTEMPTS + 1):
        try:
            async with sem:
                return await _run_search(context, filters, want_options)
        except timeout_errors():
            if attempt == PAGE_ATTEMPTS:
                raise
            print(f"  discovery search timed out; retrying ({attempt}/{PAGE_ATTEMPTS - 1})", file=sys.stderr)
            await asyncio.sleep(2 ** attempt)


async def _run_search(context, filters: list[dict], want_options: bool) -> dict:
    page = await context.new_page()
    records: dict[int, dict] = {}
    state = {"total": None, "options": None, "params": {}}
    got_batch = asyncio.Event()
    failed: list[str] = []  # "status url" for every response that came back as an HTTP error

    async def on_response(resp):
        url = resp.url
        if resp.status >= 400:
            failed.append(f"{resp.status} {url[:120]}")
        try:
            if "program/discovery-search" in url and resp.request.method == "POST":
                data = await resp.json()
                for rec in data.get("records", []):
                    records[rec["Program_Id"]] = rec
                state["total"] = data.get("totalCount", 0)
                got_batch.set()
            elif want_options and "program/search-option" in url:
                state["options"] = await resp.json()
            elif want_options and "programParams/programParam/" in url:
                data = await resp.json()
                state["params"][data["programParamId"]] = data
        except Exception:
            pass  # a response we can't parse is just skipped

    page.on("response", on_response)
    try:
        nav = await page.goto(discovery_url(filters), wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        try:
            await asyncio.wait_for(got_batch.wait(), PAGE_TIMEOUT_MS / 1000)
        except asyncio.TimeoutError:
            await report_stuck_page(page, nav.status if nav else None, failed)
            raise
        stalls = 0
        while len(records) < (state["total"] or 0) and stalls < 4:
            got_batch.clear()
            before = len(records)
            for frame in page.frames:
                if "mfe-pd" in frame.url:
                    await frame.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.mouse.wheel(0, 20000)
            try:
                await asyncio.wait_for(got_batch.wait(), 6)
            except asyncio.TimeoutError:
                pass
            stalls = stalls + 1 if len(records) == before else 0
        if want_options:
            await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
    finally:
        await page.close()

    return {"records": records, "total": state["total"], "options": state["options"], "params": state["params"]}


async def report_stuck_page(page, status: int | None, failed: list[str]) -> None:
    """Explain a search page that never loaded results, so a failed CI run says why."""
    try:
        title = await page.title()
    except Exception:
        title = "?"
    print(f"  search page loaded no results: HTTP {status}, title {title!r}, url {page.url[:120]}", file=sys.stderr)
    for line in failed[:10]:
        print(f"    error response: {line}", file=sys.stderr)
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        n = len(list(DEBUG_DIR.glob("*.png"))) + 1
        await page.screenshot(path=str(DEBUG_DIR / f"stuck-{n}.png"), full_page=True)
        (DEBUG_DIR / f"stuck-{n}.html").write_text(await page.content())
        for i, frame in enumerate(page.frames[1:], 1):
            (DEBUG_DIR / f"stuck-{n}-frame{i}.html").write_text(f"<!-- {frame.url} -->\n" + await frame.content())
    except Exception as e:
        print(f"    could not save page for debugging: {e}", file=sys.stderr)


async def complete_search(context, sem, filters: list[dict], countries: list[str], want_options: bool = False) -> dict:
    """Run a search; if paging missed results, re-run it once per country and combine.

    The site sorts by relevance and every program ties, so scrolled pages can
    overlap and skip programs. A per-country search usually fits on one page.
    """
    result = await run_search(context, sem, filters, want_options)
    total = result["total"] or 0
    if len(result["records"]) >= total:
        return result
    countries = countries or (result["options"] or {}).get("countries", [])
    parts = await asyncio.gather(*(
        run_search(context, sem, filters + [{"filterType": "programDiscoveryCountry", "filterValues": [{"value": c}]}])
        for c in countries
    ))
    for part in parts:
        result["records"].update(part["records"])
        if len(part["records"]) < (part["total"] or 0):
            print(f"  warning: a per-country search was still incomplete for {filters[-1]}", file=sys.stderr)
    if len(result["records"]) < total:
        print(f"  warning: found {len(result['records'])} of {total} results for {filters[-1]}", file=sys.stderr)
    return result


async def fetch_brochure(context, program_id: int) -> dict:
    """Open one brochure page and keep the program, brochure, and date JSON it loads."""
    page = await context.new_page()
    found: dict[str, object] = {}
    done = asyncio.Event()
    needed = BROCHURE_PARTS

    async def on_response(resp):
        url = resp.url
        if "/models/services/REST/" not in url:
            return
        try:
            if f"/v3/program/{program_id}/brochure" in url:
                found["brochure"] = await resp.json()
            elif f"endpoint=/program&id={program_id}" in url:
                found["program"] = await resp.json()
            elif "endpoint=/program/dateSummary" in url:
                found["dates"] = await resp.json()
            elif "endpoint=/documentCenter" in url:
                data = await resp.json()
                found.setdefault("documents", {})[str(data.get("FILE_ID"))] = data
        except Exception:
            return
        if all(k in found for k in needed):
            done.set()

    page.on("response", on_response)
    try:
        await page.goto(BROCHURE_URL.format(program_id), wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        await asyncio.wait_for(done.wait(), PAGE_TIMEOUT_MS / 1000)
        await page.wait_for_timeout(1500)  # let shared document blocks finish loading
    except timeout_errors():
        pass  # the caller checks what's missing and retries
    finally:
        await page.close()
    return found


async def scrape(cache: Path, concurrency: int) -> None:
    from playwright.async_api import async_playwright

    cache.mkdir(parents=True, exist_ok=True)
    (cache / "brochures").mkdir(exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1400, "height": 1000})
        sem = asyncio.Semaphore(concurrency)  # limits open browser tabs

        print("Listing Dialogue of Civilizations programs...")
        base = await complete_search(context, sem, [DOC_FILTER], [], want_options=True)
        programs = base["records"]
        # Countries that actually have Dialogues; used to split later searches.
        doc_countries = sorted({loc["country"] for rec in programs.values()
                                for loc in rec.get("stLocations") or [] if loc.get("country")})
        print(f"  {len(programs)} programs (site reports {base['total']})")
        if not programs:
            sys.exit("No programs found; the portal may have changed.")

        # Which filter values exist, as listed by the site's filter panel.
        filter_values: dict[int, list[str]] = {}
        for pid in LIST_FILTERS:
            param = base["params"].get(pid)
            if param is None and base["options"]:
                param = next(
                    (p for p in base["options"].get("programParameters", []) if p["programParamId"] == pid), None
                )
            values = [v["paramValue"] for v in (param or {}).get("programParamValues") or []]
            if not values:
                print(f"  warning: no values found for filter {LIST_FILTERS[pid]}", file=sys.stderr)
            filter_values[pid] = values

        async def match(column: str, value: str, extra: dict) -> tuple[str, str, list[int]]:
            result = await complete_search(context, sem, [DOC_FILTER, extra], doc_countries)
            return column, value, sorted(result["records"])

        jobs = []
        for pid, column in LIST_FILTERS.items():
            for value in filter_values[pid]:
                extra = {"filterType": "programDiscoveryProgramParameters", "filterValues": [{"id": pid, "value": value}]}
                jobs.append(match(column, value, extra))
        for column, extra in TOGGLE_FILTERS.items():
            jobs.append(match(column, "Yes", extra))

        print(f"Applying {len(jobs)} filter values...")
        memberships: dict[str, dict[str, list[int]]] = {}
        for i, coro in enumerate(asyncio.as_completed(jobs), 1):
            column, value, ids = await coro
            memberships.setdefault(column, {})[value] = ids
            if i % 20 == 0 or i == len(jobs):
                print(f"  {i}/{len(jobs)}")

        async def brochure(pid: int):
            path = cache / "brochures" / f"{pid}.json"
            for attempt in range(1, PAGE_ATTEMPTS + 1):
                async with sem:
                    data = await fetch_brochure(context, pid)
                missing = [k for k in BROCHURE_PARTS if k not in data]
                if not missing:
                    break
                if attempt < PAGE_ATTEMPTS:
                    await asyncio.sleep(2 ** attempt)
            else:
                if path.exists():
                    print(f"  warning: program {pid} brochure missing {missing}; keeping the last saved copy",
                          file=sys.stderr)
                    return
                print(f"  warning: program {pid} brochure missing {missing}", file=sys.stderr)
            path.write_text(json.dumps(data))

        print(f"Fetching {len(programs)} brochures...")
        await asyncio.gather(*(brochure(pid) for pid in programs))

        print("Fetching the GEO DOC Course Guide (NUPaths, minimum GPA)...")
        try:
            guide = await fetch_course_guide(context)
            (cache / "course_guide.html").write_text(guide)
        except Exception as e:
            print(f"  warning: could not fetch {COURSE_GUIDE_URL} ({e}); NUPaths and GPA will use the last saved copy",
                  file=sys.stderr)

        await browser.close()

    print("Fetching the Honors Program's list of Honors-Endorsed Dialogues...")
    try:
        (cache / "honors.html").write_text(fetch_honors_page())
    except OSError as e:
        print(f"  warning: could not fetch {HONORS_URL} ({e}); honors_endorsed will use the last saved copy")

    (cache / "programs.json").write_text(json.dumps(list(programs.values())))
    (cache / "filters.json").write_text(json.dumps(memberships, indent=1))


# --------------------------------------------------------------------------- parsing

COURSE_RE = re.compile(r"\b([A-Z]{2,5})\s?-?\s?(\d{4})\b")
AMOUNT = r"\d{1,3}(?:,\s?\d{3})+|\d+"  # tolerates "$17, 768" typos
MONEY_RE = re.compile(rf"\$\s?({AMOUNT})")


def html_to_text(fragment: str) -> str:
    fragment = re.sub(r"(?is)<(script|style).*?</\1>", "", fragment or "")
    fragment = re.sub(r"(?i)<li[^>]*>", "\n- ", fragment)
    fragment = re.sub(r"(?i)<(br|/p|/div|/li|/h\d|/tr|/ul|/ol|/table)[^>]*>", "\n", fragment)
    fragment = re.sub(r"(?i)<t[dh][^>]*>", " | ", fragment)
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    text = text.replace("\u200b", "").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def section_texts(raw: dict) -> dict[str, str]:
    """Brochure tab name -> plain text of its content blocks (including shared documents)."""
    out: dict[str, str] = {}
    docs = raw.get("documents", {})
    sections = ((raw.get("brochure") or {}).get("current") or {}).get("sections") or []
    for section in sections:
        parts = []
        for widget in section.get("sectionWidgets") or []:
            if widget.get("contentHTML"):
                parts.append(html_to_text(widget["contentHTML"]))
            media = widget.get("contentMedia") or {}
            if isinstance(media, dict) and media.get("type") == "wysiwyg":
                doc = docs.get(str(media.get("fileId")))
                if doc:
                    parts.append(html_to_text(doc.get("WYSIWYG_CONTENT", "")))
        out[section.get("sectionDisplayName", "").strip()] = "\n".join(p for p in parts if p)
    return out


def find_section(sections: dict[str, str], *keywords: str) -> str:
    for name, text in sections.items():
        if any(k in name.lower() for k in keywords):
            return text
    return ""


def parse_faculty(text: str) -> tuple[list[str], list[str]]:
    """Names and emails listed under 'Faculty Leader(s)', stopping at the GEO contact."""
    m = re.search(r"(?is)faculty\s+(?:leaders?|directors?|contacts?)\s*:?(.*?)(?:\bGEO\b|Global Experience Office|$)", text)
    if not m:
        return [], []
    block = m.group(1)
    names = [n.strip(" :-,") for n in re.findall(r"(?im)^\s*-?\s*Name\s*:\s*(.+)$", block)]
    emails = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", block)
    if not names:
        # Unlabelled lists: lines that aren't emails, phone numbers, or labels.
        for line in block.splitlines():
            line = line.strip(" -:")
            if not line or "@" in line or re.search(r"\d{3}", line) or line.lower().startswith(("email", "phone")):
                continue
            if len(line.split()) <= 6:
                names.append(re.sub(r"(?i)^name\s*:\s*", "", line))
    return list(dict.fromkeys(names)), list(dict.fromkeys(emails))


CODE = r"[A-Z]{2,5}\s?\d{4}[A-Z]?\*?"
# A line that starts with one or more course codes ("POLS 3430 / INTL 3430: Title").
COURSE_LINE_RE = re.compile(rf"^-?\s*((?:{CODE}\s*(?:/|,|&|\bor\b)\s*)*{CODE})\s*(?:[:\-–—|]\s*|\s+|$)(.*)$")
# Lines about other courses: prerequisites, recommendations, equivalencies.
NOT_OFFERED_RE = re.compile(
    r"prereq|co-?req|minimum grade|recommended|prepar|prepat|can count|counts? (?:for|as)|equivalen|"
    r"must have|completion of|taken|satisf|fulfil", re.I)


def parse_courses(text: str) -> list[str]:
    """'SUBJ 1234: Title' for each course offered, skipping prerequisite mentions."""
    courses: dict[str, str] = {}
    for line in text.splitlines():
        if NOT_OFFERED_RE.search(line):
            continue
        m = COURSE_LINE_RE.match(line)
        if not m:
            continue
        codes = re.sub(r"\s+", " ", m.group(1).replace("*", "")).strip()
        codes = re.sub(r"([A-Z])(\d)", r"\1 \2", codes)
        title = re.split(r"(?<=[a-z)])\.\s|\s\(|\s\|", m.group(2).strip())[0].strip(" :-–—")
        title = title if 2 < len(title) <= 100 else ""
        key = codes.split(" /")[0].split(",")[0]
        if key not in courses or (not courses[key][1] and title):
            courses[key] = (codes, title)
    if not courses:
        # Some brochures only name courses inside paragraphs ("...the first course, LING 3150: Field Linguistics").
        for line in text.splitlines():
            if NOT_OFFERED_RE.search(line):
                continue
            for m in re.finditer(rf"\b({CODE})\b(?:\s*[:\-–—]\s*([A-Z][^.:;(\n|,]{{2,80}}))?", line, re.I):
                code = re.sub(r"\s*(\d)", r" \1", m.group(1).upper(), count=1).replace("*", "")
                if code.split()[0] in {"GPA", "USD", "MAY", "JUNE", "JULY"}:
                    continue
                if code not in courses or (not courses[code][1] and m.group(2)):
                    courses[code] = (code, (m.group(2) or "").strip())
    return [f"{codes}: {title}" if title else codes for codes, title in courses.values()]


PREREQ_LABEL_RE = re.compile(
    r"^-?\s*(?:Note:\s*)?(recommended prep\w* courses?|pre-?requisites?(?:\(s\))?)\s*:?\s*(.*)$", re.I)
GRADE_RE = re.compile(r"\s*with a minimum grade of ([A-D][+-]?)", re.I)


def _course_key(code: str) -> str:
    return re.sub(r"([A-Z])\s*(\d)", r"\1 \2", code.replace("*", "").upper())


def _tidy_prereq(text: str) -> str:
    """'X with a minimum grade of C- or Y with a minimum grade of C-' -> 'X or Y (min grade C-)'."""
    grades = set(g.upper() for g in GRADE_RE.findall(text))
    if len(grades) == 1:
        text = GRADE_RE.sub("", text) + f" (min grade {grades.pop()})"
    else:
        text = GRADE_RE.sub(lambda m: f" ({m.group(1).upper()})", text)
    text = text.replace("(may be taken concurrently)", "(can be concurrent)")
    text = re.sub(r"\s*;\s*", " and ", text)  # "; " separates values in the CSV
    text = re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", text))
    return re.sub(r"\s+", " ", text).strip(" .")


def parse_prerequisites(academics: str, overview: str, offered: list[str]) -> str:
    """Prerequisites per course. Leveled courses (language tiers) only list the lowest level."""
    offered_keys = {_course_key(c.split(":")[0].split(" /")[0]) for c in offered}
    per_course: dict[str, list[tuple[str, str]]] = {}  # course -> [(kind, text)]
    flexible = False
    current, pending, after_or = None, None, False
    groups: list[list[str]] = []  # courses offered as alternatives ("... OR ...")
    for line in academics.splitlines():
        if re.fullmatch(r"OR\b.*", line.strip()):
            after_or = True
            continue
        if "flexible per faculty review" in line.lower():
            flexible = True
        label = PREREQ_LABEL_RE.match(line)
        if label:
            kind = "Recommended" if label.group(1).lower().startswith("rec") else "Required"
            if label.group(2).strip():
                per_course.setdefault(current, []).append((kind, label.group(2).strip()))
            else:
                pending = kind  # "Prerequisites" heading, value on the next line
            continue
        if pending:
            per_course.setdefault(current, []).append((pending, line.strip(" -")))
            pending = None
            continue
        if re.search(r"\(no prerequisites\)", line, re.I):
            continue
        m = COURSE_LINE_RE.match(line)
        if m and not NOT_OFFERED_RE.search(line):
            current = _course_key(re.split(r"\s*(?:/|,|&|\bor\b)\s*", m.group(1))[0])
            if after_or and groups:
                groups[-1].append(current)
            else:
                groups.append([current])
            after_or = False

    items: dict[str, list[str]] = {}
    for course, entries in per_course.items():
        for kind, text in entries:
            if re.search(r"waived|no prerequisites|^none\b", text, re.I):
                continue
            codes = {_course_key(c) for c in re.findall(CODE, text)}
            if codes and codes <= offered_keys:
                continue  # satisfied by another course on the same program
            if not codes and "concurrent" in text.lower():
                continue
            items.setdefault(course or "", []).append(("Recommended: " if kind == "Recommended" else "") + _tidy_prereq(text))

    # Leveled offerings (e.g. Japanese 2301 OR 2302 OR 3301): students take one level, so only the
    # lowest level's prerequisite is the program minimum.
    leveled = {}
    for group in groups:
        options = [c for c in group if c in items]
        if len(group) >= 2 and options:
            lowest = min(group, key=lambda c: c.split(" ")[-1])
            leveled[lowest] = len(group)
            for c in options:
                if c != lowest:
                    items.pop(c)

    out = []
    for course, texts in items.items():
        label = f"{course} (lowest of {leveled[course]} levels)" if course in leveled else course
        for t in dict.fromkeys(texts):
            out.append(f"{label}: {t}" if label else t)
    m = re.search(r"Program Prerequisites:\s*(Not open to students who have already taken [^.]+)", overview, re.I)
    if m:
        out.append(m.group(1).strip())
    if out and flexible:
        out.append("Exceptions possible per faculty review")
    return "; ".join(out) or "None"


def parse_credits(text: str) -> str:
    m = re.search(r"(\d+)\s+(?:Northeastern\s+|NU\s+|semester\s+)?credits?", text, re.I)
    return m.group(1) if m else ""


def money(s: str) -> float:
    return float(re.sub(r"[,\s]", "", s))


def parse_costs(text: str) -> dict:
    """Pull dollar amounts from the Costs tab into named columns plus a total."""
    out = {"tuition": "", "doc_fee": "", "flight_estimate": "", "food_estimate": "",
           "housing_estimate": "", "personal_expenses_estimate": "", "other_costs": "",
           "estimated_total_cost": "", "cost_lines": ""}
    lines, total, other = [], 0.0, []
    for line in text.splitlines():
        amounts = MONEY_RE.findall(line)
        if not amounts:
            continue
        label = line.split(":")[0].strip(" -").lower() if ":" in line else line.lower()
        # Ranges ("$650-700", "$1,000 to $1,500") count at their upper end.
        rng = re.search(rf"\$\s?({AMOUNT})\s*(?:-|–|to)\s*\$?\s?({AMOUNT})", line)
        value = money(rng.group(2)) if rng else money(amounts[0])
        lines.append(line.strip(" -"))
        if re.search(r"scholarship|grant|deposit|withdraw|refund|penalt|cancel|forfeit|award", line, re.I):
            continue  # not a cost of attending
        if "tuition" in label and not out["tuition"]:
            key = "tuition"
        elif ("fee" in label and ("dialogue" in label or "doc" in label or "program" in label)) and not out["doc_fee"]:
            key = "doc_fee"
        elif ("flight" in label or "airfare" in label) and not out["flight_estimate"]:
            key = "flight_estimate"
        elif ("food" in label or "meal" in label) and not out["food_estimate"]:
            key = "food_estimate"
        elif ("housing" in label or "accommodation" in label) and not out["housing_estimate"]:
            key = "housing_estimate"
        elif "personal" in label and not out["personal_expenses_estimate"]:
            key = "personal_expenses_estimate"
        else:
            other.append(f"{line.split(':')[0].strip(' -')[:60]}: ${value:,.0f}")
            total += value
            continue
        out[key] = f"{value:.0f}"
        total += value
    out["other_costs"] = "; ".join(other)
    out["estimated_total_cost"] = f"{total:.0f}" if total else ""
    out["cost_lines"] = " | ".join(lines)
    return out


ENDORSED_RE = re.compile(r"\b((?:[A-Z][\w'’.]*\s+){0,6}?[A-Z][\w'’.]*)\s*-\s*Endorsed\b")


def parse_endorsements(text: str) -> list[str]:
    """College names from phrases like 'Honors-Endorsed Program' or 'College of Science-Endorsed'."""
    found = []
    for m in ENDORSED_RE.finditer(text):
        name = re.sub(r"^(?:An?|The|This|Is|For)\s+", "", m.group(1).strip())
        name = "Honors" if "honors" in name.lower() else name
        if name not in found:
            found.append(name)
    return found


async def fetch_course_guide(context) -> str:
    """The Course Guide is a WordPress TablePress table. The site's firewall refuses plain HTTP clients, so it's
    loaded in the browser; the server's HTML has every row (the page's own script later hides all but 25)."""
    page = await context.new_page()
    try:
        resp = await page.goto(COURSE_GUIDE_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        if not resp or not resp.ok:
            raise RuntimeError(f"HTTP {resp.status if resp else '?'}")
        body = await resp.text()
    finally:
        await page.close()
    if not parse_course_guide(body):
        raise RuntimeError("no Course Guide table found; the page may have changed")
    return body


def parse_course_guide(page_html: str) -> dict[str, dict]:
    """program id -> {"course_nupaths": {course code: [NUPath codes]}, "min_gpa": "2.50"} from the Course Guide."""
    table = re.search(r'(?s)<table[^>]*id="tablepress-[^"]*"[^>]*>.*?</table>', page_html)
    if not table or "<tbody" not in table[0]:
        return {}
    head, body = table[0].split("<tbody", 1)
    headers = [html_to_text(h).strip().lower() for h in re.findall(r"(?s)<th[^>]*>(.*?)</th>", head)]
    try:
        details_col, gpa_col = headers.index("course details"), headers.index("minimum gpa")
    except ValueError:
        return {}
    out = {}
    for tr in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", body):
        pid = re.search(r"programid=(\d+)", tr)
        cells = re.findall(r"(?s)<td[^>]*>(.*?)</td>", tr)
        if not pid or len(cells) <= max(details_col, gpa_col):
            continue
        course_nupaths = {}
        # Each course is "<li><strong>Course:</strong> CODE: Title <ul><li>NUPaths: IC, EI</li>...</ul></li>".
        for block in re.split(r"<strong>Course:</strong>", cells[details_col])[1:]:
            code = COURSE_RE.search(html_to_text(block.split("<ul>")[0]))
            nupaths = re.search(r"(?s)NUPaths:</strong>(.*?)</li>", block)
            if code and nupaths:
                codes = [c for c in re.findall(r"\b[A-Z]{2}\b", html_to_text(nupaths[1])) if c in NUPATHS]
                if codes:
                    course_nupaths[f"{code[1]} {code[2]}"] = list(dict.fromkeys(codes))
        gpa = re.search(r"\d\.\d+", html_to_text(cells[gpa_col]))
        out[pid[1]] = {"course_nupaths": course_nupaths, "min_gpa": gpa[0] if gpa else ""}
    return out


def fetch_honors_page() -> str:
    # A plain HTTP request: the Honors site hangs for headless Chromium.
    import urllib.request

    req = urllib.request.Request(HONORS_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; nu-dialogues-scraper)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_honors_list(page_html: str) -> list[tuple[str, str]]:
    """(title, faculty line) for each current Honors-Endorsed DOC on the Honors Program page.
    Only the current list counts, not "Previous Honors DOC Offerings"."""
    text = html_to_text(page_html)
    m = re.search(r"(?s)Honors-Endorsed DOCs\n(.*?)\nPrevious Honors", text)
    if not m:
        return []
    lines = [l for l in m.group(1).splitlines() if len(l) < 200 and not re.fullmatch(r"Summer \d", l)]
    return list(zip(lines[::2], lines[1::2]))


def _title_words(title: str) -> set[str]:
    title = re.sub(r"^Dialogue\+?:\s*", "", html.unescape(title)).lower().replace("&", " and ")
    return set(re.findall(r"[a-z]+", title)) - {"and", "the", "of", "in", "a", "an", "to", "for", "through"}


def match_honors(entries: list[tuple[str, str]], rows: list[dict]) -> set:
    """Program ids for the Honors page's entries, matched on title words plus faculty surname
    (the page's titles are shortened, e.g. "Software Development" for "Software Engineering")."""
    matched = set()
    for title, faculty in entries:
        words = _title_words(title)
        name = re.sub(r"^(?:Prof\.?|Professor|Dr\.?)\s+", "", faculty.split(",")[0].strip())
        surname = name.split()[-1].lower() if name else ""

        def score(row):
            overlap = len(words & _title_words(row["program_name"])) / max(len(words), 1)
            return overlap + (0.5 if surname and surname in row["faculty_leaders"].lower() else 0)

        best = max(rows, key=score)
        if score(best) >= 0.6:
            matched.add(best["program_id"])
        else:
            print(f"  warning: no Dialogue matches Honors listing {title!r} ({faculty})")
    return matched


def fmt_date(iso: str | None) -> str:
    return (iso or "")[:10]


DATE_WORDS = r"[A-Z][a-z]+\.? \d{1,2},? \d{4}"


def iso_date(s: str) -> str:
    """'May 10, 2027' -> '2027-05-10'; returned unchanged if it can't be parsed."""
    from datetime import datetime

    cleaned = s.replace(".", "").replace(",", "")
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


def dates_from_text(text: str) -> tuple[str, str, str]:
    """Fallback for brochures without a dates table. Returns (start, end, application deadline) from
    lines like 'Program Dates: May 10, 2027 - June 11, 2027' and 'Application Deadline: October 1, 2026'."""
    start = end = deadline = ""
    m = re.search(rf"Program Dates?\s*:?\s*({DATE_WORDS})\s*(?:-|–|—|to)\s*({DATE_WORDS})", text)
    if m:
        start, end = iso_date(m[1]), iso_date(m[2])
    m = re.search(rf"Application Deadline\s*:?\s*({DATE_WORDS})", text)
    if m:
        deadline = iso_date(m[1])
    return start, end, deadline


def previous_values(csv_path: Path | None, columns: list[str]) -> dict[str, dict]:
    """program id -> {column: value} from the last CSV, for sources that couldn't be fetched this run.
    cache/ isn't kept between GitHub Actions runs, so without this a blocked page would blank its columns."""
    if not csv_path or not csv_path.exists():
        return {}
    with csv_path.open(newline="", encoding="utf-8") as f:
        return {r["program_id"]: {c: r.get(c, "") for c in columns} for r in csv.DictReader(f)}


def build_rows(cache: Path, previous_csv: Path | None = None) -> list[dict]:
    programs = json.loads((cache / "programs.json").read_text())
    memberships = json.loads((cache / "filters.json").read_text())

    rows = []
    for rec in sorted(programs, key=lambda r: r["Program_Name"]):
        pid = rec["Program_Id"]
        path = cache / "brochures" / f"{pid}.json"
        raw = json.loads(path.read_text()) if path.exists() else {}
        sections = section_texts(raw)
        program = raw.get("program") or {}
        dates = sorted(raw.get("dates") or [], key=lambda d: d.get("STARTDATE") or "")

        locations = program.get("stLocations") or rec.get("stLocations") or []
        overview = find_section(sections, "overview")
        academics = find_section(sections, "academic", "course")
        costs_text = find_section(sections, "cost", "financ", "budget")
        all_text = "\n".join(sections.values())

        text_start, text_end, text_deadline = dates_from_text(all_text)
        names, emails = parse_faculty(overview) if overview else ([], [])
        if not names:
            names, emails = parse_faculty(all_text)

        courses = parse_courses(academics or all_text)
        row = {
            "program_id": pid,
            "program_name": html.unescape(rec["Program_Name"]).strip(),
            "courses": "; ".join(courses),
            "prerequisites": parse_prerequisites(academics or all_text, overview, courses),
            "credits": parse_credits(costs_text + "\n" + academics),
            "faculty_leaders": "; ".join(names),
            "faculty_emails": "; ".join(emails),
            "term": "; ".join(program.get("TERMS") or rec.get("Terms") or []),
            "year": "; ".join(str(d.get("YEAR", "")) for d in dates) or text_start[:4],
            "start_date": "; ".join(fmt_date(d.get("STARTDATE")) for d in dates) or text_start,
            "end_date": "; ".join(fmt_date(d.get("ENDDATE")) for d in dates) or text_end,
            "application_deadline": "; ".join(fmt_date(d.get("APPLICATIONDEADLINE")) for d in dates) or text_deadline,
            "decision_date": "; ".join(fmt_date(d.get("DECISIONDATE")) for d in dates),
            "cities": "; ".join(loc.get("city") or "" for loc in locations),
            "countries": "; ".join(dict.fromkeys(loc.get("country") or "" for loc in locations)),
            "regions": "; ".join(dict.fromkeys(loc.get("region") or "" for loc in locations if loc.get("region"))),
        }
        row.update(parse_costs(costs_text))
        for column in list(LIST_FILTERS.values()):
            values = memberships.get(column, {})
            # Sorted: memberships are saved in the order searches finished, which varies by run.
            row[column] = "; ".join(sorted(v for v, ids in values.items() if pid in ids))
        for column in TOGGLE_FILTERS:
            if column in memberships:
                row[column] = "Yes" if pid in memberships[column].get("Yes", []) else "No"
            else:
                row[column] = ""
        # Endorsements: the portal's College-Endorsed filter, plus "<College>-Endorsed Program"
        # notes in the brochure. Honors comes only from the Honors Program's own list (below).
        endorsed = [v for v in row["college_endorsed"].split("; ") if v]
        endorsed += [e for e in parse_endorsements(all_text) if e not in endorsed and e != "Honors"]
        row["college_endorsed"] = "; ".join(endorsed)
        row["url"] = BROCHURE_URL.format(pid)
        rows.append(row)

    guide_page = cache / "course_guide.html"
    guide = parse_course_guide(guide_page.read_text()) if guide_page.exists() else {}
    if not guide:
        kept = previous_values(previous_csv, ["nupaths", "course_nupaths", "min_gpa"])
        print(f"  warning: no Course Guide data in {guide_page}; "
              + ("keeping nupaths and min_gpa from the last CSV" if kept else "nupaths and min_gpa will be empty"),
              file=sys.stderr)
    for row in rows:
        if not guide:
            row.update(kept.get(str(row["program_id"]), {"nupaths": "", "course_nupaths": "", "min_gpa": ""}))
            continue
        g = guide.get(str(row["program_id"]), {})
        per_course = g.get("course_nupaths", {})
        # Sorted in NUPATHS order so the CSV is stable from run to run.
        union = [c for c in NUPATHS if any(c in v for v in per_course.values())]
        row["nupaths"] = "; ".join(union)
        row["course_nupaths"] = "; ".join(f"{course}: {', '.join(codes)}" for course, codes in per_course.items())
        row["min_gpa"] = g.get("min_gpa", "")

    honors_page = cache / "honors.html"
    honors = match_honors(parse_honors_list(honors_page.read_text()), rows) if honors_page.exists() else set()
    if not honors:
        kept_honors = previous_values(previous_csv, ["honors_endorsed"])
        honors = {pid for pid, v in kept_honors.items() if v["honors_endorsed"] == "Yes"}
        honors = {row["program_id"] for row in rows if str(row["program_id"]) in honors}
        print(f"  warning: no Honors list in {honors_page}; "
              + ("keeping honors_endorsed from the last CSV" if honors else "honors_endorsed will be all No"), file=sys.stderr)
    for row in rows:
        row["honors_endorsed"] = "Yes" if row["program_id"] in honors else "No"
        if row["program_id"] in honors:
            row["college_endorsed"] = "; ".join([v for v in row["college_endorsed"].split("; ") if v] + ["Honors"])
        row["url"] = row.pop("url")  # keep url as the last column
    return rows


def write_csv(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_ui_data(rows: list[dict], out: Path) -> None:
    """Data for ui/index.html, as a script so the page also works opened straight from disk."""
    from datetime import datetime, timezone

    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "nupaths": NUPATHS, "programs": rows}
    out.write_text("window.DIALOGUES = " + json.dumps(payload, ensure_ascii=False) + ";\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output", type=Path, default=Path("dialogues.csv"), help="CSV path (default: dialogues.csv)")
    ap.add_argument("--cache-dir", type=Path, default=Path("cache"), help="where raw page data is saved (default: cache)")
    ap.add_argument("--from-cache", action="store_true", help="rebuild the CSV from --cache-dir without scraping")
    ap.add_argument("--ui-data", type=Path, default=Path("ui/data.js"),
                    help="data file for the browser UI (default: ui/data.js)")
    ap.add_argument("--concurrency", type=int, default=4, help="browser tabs open at once (default: 4)")
    args = ap.parse_args()

    if not args.from_cache:
        asyncio.run(scrape(args.cache_dir, max(1, args.concurrency)))
    elif not (args.cache_dir / "programs.json").exists():
        sys.exit(f"No cache at {args.cache_dir}; run without --from-cache first.")

    rows = build_rows(args.cache_dir, previous_csv=args.output)
    write_csv(rows, args.output)
    print(f"Wrote {len(rows)} programs to {args.output}")
    if args.ui_data:
        write_ui_data(rows, args.ui_data)
        print(f"Wrote browser data to {args.ui_data}")


if __name__ == "__main__":
    main()
