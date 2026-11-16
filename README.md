# Northeastern Dialogues

**[praneel-g.github.io/nu-dialogues](https://praneel-g.github.io/nu-dialogues/)**

Every Dialogue of Civilizations (DOC) program on Northeastern's GEO portal in
one searchable table: courses, prerequisites, faculty, dates, DOC fee, and
estimated total cost. The scraper also writes everything to `dialogues.csv`.

> Unofficial student tool, not affiliated with Northeastern GEO. Values are
> pulled from program brochures, which can change. Check the brochure link in
> each row before relying on a number.

## Using the page

- For fields with several values per program (faculty, subjects, colleges,
  countries), **Any** matches programs with at least one selected value and
  **All** matches programs with every one.
- Filters are saved in the URL, so you can bookmark or share a view.
- **Export CSV** downloads only the programs shown.
- **☆** stars a program. Stars are saved in your browser on that device only;
  **Starred only** under Quick filters shows them.
- **Apply ↗** opens the program's GEO application (after Northeastern sign-in).
  It only appears while the portal says the program is accepting applications.

On a phone, **Add to Home Screen** installs it as an app that also works
offline. `ui/index.html` also works opened straight from disk.

## Running the scraper

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium

.venv/bin/python scrape_dialogues.py                 # scrape (~10 min), writes dialogues.csv and ui/data.js
.venv/bin/python scrape_dialogues.py --from-cache    # rebuild from cache/, no network
```

`-o` sets the CSV path and `--concurrency` the number of browser tabs (default 4).
A GitHub Action rescrapes every Monday and redeploys the site if the data
changed. Pushing to `ui/` on `main` also redeploys.

## Columns

Columns with several values are separated by `; `. Dates are YYYY-MM-DD and costs are in USD.

| Column | Source |
| --- | --- |
| `program_name`, `term`, `cities`, `countries`, `regions` | Program record |
| `courses`, `credits` | Academics tab: lines that start with a course code |
| `prerequisites` | Academics tab "Prerequisite(s)" lines, by course. Where you pick one of several levels (e.g. Japanese), only the lowest is listed. Waived prerequisites, and ones met by another course on the same Dialogue, are left out. `None` if nothing is required. |
| `faculty_leaders`, `faculty_emails` | "Faculty Leaders" contact block |
| `start_date`, `end_date`, `application_deadline`, `decision_date`, `year` | Dates/Deadlines table, or "Program Dates" and "Application Deadline" lines in the brochure if it has none |
| `tuition`, `doc_fee`, `flight_estimate`, `food_estimate`, `housing_estimate`, `personal_expenses_estimate`, `other_costs` | Costs tab |
| `estimated_total_cost` | Sum of the cost columns. A price range counts at its upper end. Scholarships, deposits, and withdrawal penalties are left out. |
| `cost_lines` | Every Costs tab line with a dollar amount, for checking the parsed numbers |
| `colleges_and_schools`, `academic_subjects`, `program_status`, `institution_type` | Portal filters |
| `college_endorsed` | Portal College-Endorsed filter (currently matches nothing), "&lt;College&gt;-Endorsed Program" notes in the brochure, plus Honors |
| `honors_endorsed` | Yes if on the Honors Program's [current Honors-Endorsed DOC list](https://honors.northeastern.edu/global-2/global-opportunities/dialogues-of-civilization/) |
| `accepting_applications`, `program_sponsored_housing`, `us_accredited_institution` | Portal on/off filters (Yes/No) |
| `url` | Program brochure |

Some filter columns, like `institution_type`, are empty for every Dialogue
because the portal shares its filters across all program types.

## How it works

The portal is a JavaScript app, so the script opens it in headless Chromium,
visits the same public pages a student would, and saves the JSON they load to
`cache/`. The portal pages results 12 at a time and Dialogues all tie on
relevance, so paging can skip programs. When a search comes back short, the
script repeats it per country and merges the results.
