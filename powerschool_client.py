import re
import json
from urllib.parse import urljoin
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from bs4 import BeautifulSoup

BASE_URL = "https://ma.powerschool.com"

# Morrison Academy SBG grading scale (from Excel)
GRADE_SCALE = [
    ("A+", 8.50, 9.00, 4.0),
    ("A",  7.56, 8.49, 4.0),
    ("A-", 7.00, 7.55, 3.7),
    ("B+", 6.44, 6.99, 3.3),
    ("B",  5.89, 6.43, 3.0),
    ("B-", 5.33, 5.88, 2.7),
    ("C+", 4.78, 5.32, 2.3),
    ("C",  4.22, 4.77, 2.0),
    ("C-", 3.67, 4.21, 1.7),
    ("D+", 3.11, 3.66, 1.3),
    ("D",  2.56, 3.10, 1.0),
    ("D-", 2.00, 2.55, 0.7),
    ("F",  0.00, 1.99, 0.0),
]

LETTER_TO_GPA = {
    "A+": 4.0, "A": 4.0, "A-": 3.7,
    "B+": 3.3, "B": 3.0, "B-": 2.7,
    "C+": 2.3, "C": 2.0, "C-": 1.7,
    "D+": 1.3, "D": 1.0, "D-": 0.7,
    "F": 0.0,
}


def sbg_to_letter(score: float) -> str:
    for letter, low, high, _ in GRADE_SCALE:
        if low <= score <= high:
            return letter
    return "F"


def sbg_to_gpa(score: float) -> float:
    for _, low, high, gpa in GRADE_SCALE:
        if low <= score <= high:
            return gpa
    return 0.0


class PowerSchoolClient:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._page = None
        self._cached_student_id = None  # reused across all courses once found

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        self._page = await self._browser.new_page()

    async def login(self, username: str, password: str):
        page = self._page
        await page.goto(f"{BASE_URL}/public/")
        await page.wait_for_selector("input[name='account']", timeout=10000)
        await page.fill("input[name='account']", username)
        await page.fill("input[name='pw']", password)
        await page.keyboard.press("Enter")

        try:
            await page.wait_for_url(f"{BASE_URL}/guardian/home.html", timeout=10000)
        except PlaywrightTimeout:
            content = await page.content()
            if "pslogin" in content or "Sign In" in await page.title():
                raise Exception("Invalid username or password")
            raise Exception("Login timed out — try again")

        content = await page.content()
        if 'class="pslogin"' in content or 'id="pslogin"' in content:
            raise Exception("Invalid username or password")

    async def get_student_name(self) -> str:
        page = self._page
        for selector in ["#userName", ".student-name", "h1.page-heading", "h1", "h2"]:
            try:
                el = await page.query_selector(selector)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue
        return "Student"

    async def _fetch_course_standards(self, scores_url: str) -> list[dict]:
        """Navigate to a course scores page, capture the assignment API response,
        then fetch standards with SBG scores and weights."""
        page = self._page
        assignment_data = []

        async def handle_response(response):
            if "assignment/lookup" in response.url:
                try:
                    data = await response.json()
                    if isinstance(data, list):
                        assignment_data.extend(data)
                except Exception:
                    pass

        page.on("response", handle_response)
        try:
            await page.goto(scores_url)
            await page.wait_for_timeout(2000)
        finally:
            page.remove_listener("response", handle_response)

        print(f"  assignment_data count={len(assignment_data)}")
        if not assignment_data:
            print("  → no assignment data captured")
            return []

        # Collect section_id, student_id, and all assignment IDs
        section_id = None
        student_id = None
        assignment_ids = []

        for a in assignment_data:
            for sec in a.get("_assignmentsections", []):
                if section_id is None:
                    section_id = sec.get("sectionsdcid")
                scores = sec.get("_assignmentscores", [])
                if student_id is None and scores:
                    student_id = scores[0].get("studentsdcid")
            assignment_ids.append(a["assignmentid"])

        # Cache student ID once found; reuse across courses
        if student_id:
            self._cached_student_id = student_id
        elif self._cached_student_id:
            student_id = self._cached_student_id

        print(f"  section_id={section_id} student_id={student_id} assignments={len(assignment_ids)}")

        if not section_id or not student_id or not assignment_ids:
            print("  → missing IDs, skipping")
            return []

        # Call the standards API one assignment at a time in parallel (avoids "Duplicate key" server bug)
        try:
            all_results = await page.evaluate(f"""async () => {{
                const ids = {json.dumps(assignment_ids)};
                const responses = await Promise.all(ids.map(id =>
                    fetch('/ws/xte/standard/assignment', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{
                            section_ids: [{section_id}],
                            student_ids: [{student_id}],
                            assignment_ids: [id]
                        }})
                    }}).then(r => r.json()).catch(() => [])
                ));
                return responses;
            }}""")
        except Exception as e:
            print(f"  → fetch error: {e}")
            return []

        # Group all assessment entries by standard ID
        # A standard assessed multiple times → average all its scores first,
        # then use that average as the standard's score in the weighted calculation
        from collections import defaultdict
        standard_entries = defaultdict(list)

        for batch in all_results:
            if not isinstance(batch, list):
                continue
            for std in batch:
                std_id = std.get("standardid") or std.get("_id")
                if not std_id:
                    continue
                score_data = std.get("_score")
                if not score_data:
                    continue
                score = score_data.get("scorenumericgrade")
                if score is None:
                    continue
                assocs = std.get("_standardcourseassociations", [])
                weight = assocs[0]["weight"] if assocs else 1
                standard_entries[std_id].append({
                    "name": std.get("name", ""),
                    "score": float(score),
                    "weight": float(weight),
                })

        # Build final standards list: one entry per unique standard, with averaged score
        standards = []
        for std_id, entries in standard_entries.items():
            avg_score = sum(e["score"] for e in entries) / len(entries)
            weight = entries[0]["weight"]
            standards.append({
                "name": entries[0]["name"],
                "score": avg_score,
                "weight": weight,
            })

        return standards

    async def get_grades(self) -> list[dict]:
        page = self._page
        await page.wait_for_load_state("networkidle", timeout=10000)
        content = await page.content()
        soup = BeautifulSoup(content, "html.parser")

        grade_table = soup.find("table", class_="linkDescList")
        if not grade_table:
            return []

        rows = grade_table.find_all("tr")
        courses = []

        for row in rows[2:]:
            cells = row.find_all("td")
            if len(cells) < 24:
                continue

            def cell(i):
                return cells[i].get_text(strip=True)

            period = cell(0)
            raw_course = cell(15)
            ps_grade = cell(21)
            absences = cell(22)
            tardies = cell(23)

            course_name = re.split(r"Email\s", raw_course)[0].strip()
            if not course_name:
                continue

            # Extract scores URL from the grade link (cell 21)
            scores_url = None
            grade_link = cells[21].find("a", href=True)
            if grade_link and "scores.html" in grade_link["href"]:
                scores_url = urljoin(f"{BASE_URL}/guardian/home.html", grade_link["href"])

            courses.append({
                "period": period,
                "course": course_name,
                "ps_grade": ps_grade if ps_grade else "—",
                "absences": absences,
                "tardies": tardies,
                "scores_url": scores_url,
            })

        # Fetch standards and calculate for each course
        for course in courses:
            if not course["scores_url"]:
                course.update({"avg": None, "letter": None, "gpa": None, "standards": []})
                continue
            try:
                standards = await self._fetch_course_standards(course["scores_url"])
                # If session dropped, refresh home once and retry
                if not standards:
                    await self._page.goto(f"{BASE_URL}/guardian/home.html")
                    await self._page.wait_for_load_state("networkidle", timeout=6000)
                    standards = await self._fetch_course_standards(course["scores_url"])
                if standards:
                    scored = [s for s in standards if s["score"] is not None]
                    if scored:
                        total_w = sum(s["score"] * s["weight"] for s in scored)
                        total_wt = sum(s["weight"] for s in scored)
                        avg = round(total_w / total_wt, 2) if total_wt else None
                        course["avg"] = avg
                        course["letter"] = sbg_to_letter(avg) if avg is not None else None
                        course["gpa"] = sbg_to_gpa(avg) if avg is not None else None
                        course["standards"] = scored
                        course["total_weighted_sum"] = round(total_w, 4)
                        course["total_weight"] = round(total_wt, 4)
                    else:
                        course.update({"avg": None, "letter": None, "gpa": None, "standards": []})
                else:
                    course.update({"avg": None, "letter": None, "gpa": None, "standards": []})
            except Exception as e:
                print(f"Error fetching {course['course']}: {e}")
                course.update({"avg": None, "letter": None, "gpa": None, "standards": []})

        return courses

    def calculate_gpa(self, courses: list[dict]) -> float | None:
        """Weighted GPA assuming 0.5 credits per course."""
        credits_per_course = 0.5
        valid = [(c["gpa"], credits_per_course) for c in courses if c.get("gpa") is not None]
        if not valid:
            return None
        total = sum(g * cr for g, cr in valid)
        total_cr = sum(cr for _, cr in valid)
        return round(total / total_cr, 2) if total_cr else None

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
