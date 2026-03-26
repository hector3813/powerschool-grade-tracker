from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from bs4 import BeautifulSoup

BASE_URL = "https://ma.powerschool.com"


class PowerSchoolClient:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._page = None

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
        await page.click("input[type=submit], button[type=submit]")

        try:
            await page.wait_for_url(f"{BASE_URL}/guardian/home.html", timeout=10000)
        except PlaywrightTimeout:
            content = await page.content()
            if "pslogin" in content or "Sign In" in await page.title():
                raise Exception("Invalid username or password")
            raise Exception("Login timed out — try again")

        # Double-check we're not on the login page
        content = await page.content()
        if 'class="pslogin"' in content or "id=\"pslogin\"" in content:
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

    async def get_grades(self) -> list[dict]:
        page = self._page
        await page.wait_for_load_state("networkidle", timeout=10000)
        content = await page.content()

        soup = BeautifulSoup(content, "html.parser")
        courses = []

        # The grades table has class "linkDescList grid"
        # Row 0: main headers (Exp, Last Week, This Week, Course, Q1, Q2, S1, Q3, Q4, S2, Absences, Tardies)
        # Row 1: day sub-headers (M T W H F S S M T W H F S S) — 14 day columns
        # Row 2+: data rows where:
        #   index 0  = period (Exp)
        #   index 15 = course name (includes "Email Teacher" suffix — strip it)
        #   index 21 = current grade (S2)
        #   index 22 = absences
        #   index 23 = tardies
        grade_table = soup.find("table", class_="linkDescList")
        if not grade_table:
            return courses

        rows = grade_table.find_all("tr")
        for row in rows[2:]:  # skip the two header rows
            cells = row.find_all("td")
            if len(cells) < 24:
                continue

            def cell(i):
                return cells[i].get_text(strip=True)

            period = cell(0)
            raw_course = cell(15)
            grade = cell(21)
            absences = cell(22)
            tardies = cell(23)

            # Strip "Email Teacher" suffix from course name
            import re
            course_name = re.split(r'Email\s', raw_course)[0].strip()

            if not course_name:
                continue

            courses.append({
                "Period": period,
                "Course": course_name,
                "Grade": grade if grade else "—",
                "Absences": absences,
                "Tardies": tardies,
            })

        return courses

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
