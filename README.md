# Northeastern Dialogues

**[praneel-g.github.io/nu-dialogues](https://praneel-g.github.io/nu-dialogues/)**

Every Dialogue of Civilizations (DOC) program on Northeastern's GEO portal in
one searchable table: courses, prerequisites, NUPaths, minimum GPA, faculty,
dates, DOC fee, and estimated total cost. The data is also in `dialogues.csv`.

> Unofficial student tool, not affiliated with Northeastern GEO. Values are
> pulled from program brochures, which can change. Check the brochure before
> relying on a number.

Filters are saved in the URL, so you can bookmark or share a view. Stars are
saved only in the browser you starred in. On a phone, **Add to Home Screen**
installs the site as an app that works offline.

The site has no accounts, ads, or cookies. It counts visits and feature use
anonymously through [GoatCounter](https://www.goatcounter.com).

## Running the scraper

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium

.venv/bin/python scrape_dialogues.py                 # scrape (~10 min), writes dialogues.csv and ui/data.js
.venv/bin/python scrape_dialogues.py --from-cache    # rebuild from cache/, no network
```

A GitHub Action rescrapes daily and redeploys the site when the data changes.

## Columns

Multiple values are separated by `; `. Dates are YYYY-MM-DD and costs are in USD.

| Column | Source |
| --- | --- |
| `program_name`, `term`, `cities`, `countries`, `regions` | Program record |
| `courses`, `credits` | Academics tab: lines that start with a course code |
| `prerequisites` | Academics tab "Prerequisite(s)" lines, by course. Where you pick one of several levels (e.g. Japanese), only the lowest is listed. Waived prerequisites, and ones met by another course on the same Dialogue, are left out. `None` if nothing is required. |
| `faculty_leaders`, `faculty_emails` | "Faculty Leaders" contact block |
| `start_date`, `end_date`, `application_deadline`, `decision_date`, `year` | Dates/Deadlines table, or the brochure text if it has none |
| `tuition`, `doc_fee`, `flight_estimate`, `food_estimate`, `housing_estimate`, `personal_expenses_estimate`, `other_costs` | Costs tab |
| `estimated_total_cost` | Sum of the cost columns. A price range counts at its upper end. Scholarships, deposits, and withdrawal penalties are left out. |
| `cost_lines` | Every Costs tab line with a dollar amount, for checking the parsed numbers |
| `colleges_and_schools`, `academic_subjects`, `program_status`, `institution_type` | Portal filters |
| `college_endorsed` | "&lt;College&gt;-Endorsed Program" notes in the brochure, plus Honors |
| `nupaths`, `course_nupaths`, `min_gpa` | GEO's [DOC Course Guide](https://geo.northeastern.edu/doc-courses/): NUPath codes for the program and per course (`ARTE 2500: EI, IC`), and minimum GPA |
| `honors_endorsed` | Yes if on the Honors Program's [current Honors-Endorsed DOC list](https://honors.northeastern.edu/global-2/global-opportunities/dialogues-of-civilization/) |
| `accepting_applications`, `program_sponsored_housing`, `us_accredited_institution` | Portal on/off filters (Yes/No) |
| `url` | Program brochure |

## How it works

The portal is a JavaScript app, so the script opens it in headless Chromium,
visits the same public pages a student would, and saves the JSON they load to
`cache/`. If the Course Guide or the Honors page can't be fetched, their
columns keep the values already in `dialogues.csv`.
