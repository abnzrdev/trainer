from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


class ScraperError(RuntimeError):
    """Raised when the scraper cannot complete a required operation."""


SEMESTER_START = datetime(2026, 2, 3)


async def authenticate(
    storage_state_path: str | Path = "storageState.json",
    login_url: str = "https://vjudge.net/user/login",
    timeout_ms: int = 60_000,
) -> Path:
    """Launch a headed browser, wait for manual login, and persist cookies.

    Steps:
    1. Opens VJudge login page with Chromium (headed).
    2. Waits for the user to complete login manually.
    3. Saves browser storage state (cookies/local storage) to `storage_state_path`.

    Returns:
        Path to the generated storage-state file.
    """
    output_path = Path(storage_state_path).expanduser().resolve()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeoutError as exc:
            await browser.close()
            raise ScraperError(
                f"Timed out opening login page: {login_url}"
            ) from exc

        print("Complete login in the opened browser window.")
        print("When login is successful, return here and press ENTER.")
        await asyncio.to_thread(input, "Press ENTER to save storage state... ")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(output_path))
        await browser.close()

    return output_path


class VJudgeScraper:
    """Scrape contest and problem content from VJudge using a saved auth state."""

    def __init__(
        self,
        storage_state_path: str | Path = "storageState.json",
        headless: bool = True,
        timeout_ms: int = 30_000,
        navigation_retries: int = 3,
    ) -> None:
        self.storage_state_path = Path(storage_state_path).expanduser().resolve()
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.navigation_retries = max(1, navigation_retries)

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> "VJudgeScraper":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def start(self) -> None:
        """Start Playwright browser/context with persisted auth state."""
        if not self.storage_state_path.exists():
            raise ScraperError(
                f"Storage state file not found: {self.storage_state_path}. "
                "Run authenticate() first."
            )

        if self._context is not None:
            return

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            storage_state=str(self.storage_state_path)
        )
        self._context.set_default_timeout(self.timeout_ms)

    async def close(self) -> None:
        """Close context/browser/playwright resources."""
        if self._context is not None:
            await self._context.close()
            self._context = None

        if self._browser is not None:
            await self._browser.close()
            self._browser = None

        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    def find_active_contest(self, contests: list[Any]) -> Any | None:
        days_passed = (datetime.now() - SEMESTER_START).days
        current_lecture_num = (days_passed // 7) + 1

        pattern = re.compile(r"\[.*?(26s|26spring).*?\]\s*L(\d+):\s*(.*)", flags=re.IGNORECASE)

        for contest in contests:
            if isinstance(contest, str):
                title = contest
            elif isinstance(contest, dict):
                title = str(
                    contest.get("title")
                    or contest.get("contest_title")
                    or contest.get("name")
                    or contest.get("week")
                    or ""
                )
            else:
                title = str(
                    getattr(contest, "title", "")
                    or getattr(contest, "contest_title", "")
                    or getattr(contest, "name", "")
                    or ""
                )

            match = pattern.search(title)
            if match is None:
                continue

            lecture_num = int(match.group(2))
            if lecture_num == current_lecture_num:
                return contest

        return None

    async def scrape_contest(self, contest_url: str) -> dict[str, Any]:
        """Scrape contest details and all listed problem statements.

        Returns a dictionary in this general form:
        {
          "contest_id": "12345",
          "contest_title": "...",
          "problems": [
            {
              "id": "A",
              "html_content": "<div>...</div>",
              "time_limit": "1000 MS",
              "samples": [{"in": "1 2", "out": "3"}],
              "source_url": "..."
            }
          ]
        }
        """
        await self.start()
        if self._context is None:
            raise ScraperError("Browser context is not initialized.")

        page = await self._context.new_page()
        try:
            await self._goto_with_retries(page, contest_url, wait_until="domcontentloaded")
            contest_meta = await self._extract_contest_metadata(page, contest_url)
            problems_data: list[dict[str, Any]] = []

            for problem in contest_meta["problem_refs"]:
                try:
                    problem_data = await self._scrape_problem(problem)
                    problems_data.append(problem_data)
                except PlaywrightTimeoutError:
                    problems_data.append(
                        {
                            "id": problem["id"],
                            "html_content": "",
                            "time_limit": "",
                            "samples": [],
                            "source_url": problem["url"],
                            "error": "timeout",
                        }
                    )
                except PlaywrightError as exc:
                    problems_data.append(
                        {
                            "id": problem["id"],
                            "html_content": "",
                            "time_limit": "",
                            "samples": [],
                            "source_url": problem["url"],
                            "error": f"playwright_error: {exc}",
                        }
                    )

            return {
                "contest_id": contest_meta["contest_id"],
                "contest_title": contest_meta["contest_title"],
                "problems": problems_data,
            }
        finally:
            await page.close()

    async def _goto_with_retries(
        self,
        page: Page,
        url: str,
        wait_until: str = "networkidle",
    ) -> None:
        """Navigate with retry/backoff for timeout and transient failures."""
        last_error: Exception | None = None
        for attempt in range(1, self.navigation_retries + 1):
            try:
                await page.goto(url, wait_until=wait_until, timeout=self.timeout_ms)
                return
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                last_error = exc
                if attempt < self.navigation_retries:
                    await asyncio.sleep(min(2**attempt, 6))

        if isinstance(last_error, PlaywrightTimeoutError):
            raise PlaywrightTimeoutError(f"Timed out navigating to: {url}") from last_error
        raise ScraperError(f"Failed to navigate to: {url}") from last_error

    async def _extract_contest_metadata(
        self, page: Page, contest_url: str
    ) -> dict[str, Any]:
        contest_title = (
            (await page.text_content("h1"))
            or (await page.text_content(".contest-title"))
            or (await page.title())
            or ""
        ).strip()

        contest_id = self._extract_contest_id(contest_url)

        problem_refs = await page.evaluate(
            """
            () => {
              const hrefNodes = Array.from(document.querySelectorAll('a[href*="#problem/"], a[href*="/problem/"]'));
              const refs = [];
              const seen = new Set();

              const normalize = (href) => {
                try {
                  return new URL(href, location.href).href;
                } catch {
                  return href;
                }
              };

              const idFromHref = (href) => {
                const hashMatch = href.match(/#problem\/([^/?#]+)/i);
                if (hashMatch) return hashMatch[1].trim();
                const pathMatch = href.match(/\/problem\/([^/?#]+)/i);
                if (pathMatch) return pathMatch[1].trim();
                return '';
              };

              for (const node of hrefNodes) {
                const rawHref = node.getAttribute('href') || '';
                if (!rawHref) continue;

                const fullHref = normalize(rawHref);
                const text = (node.textContent || '').trim();
                let pid = '';

                if (/^[A-Z]\d*$/.test(text)) {
                  pid = text;
                } else {
                  pid = idFromHref(rawHref) || idFromHref(fullHref);
                }

                if (!pid) continue;

                const dedupeKey = `${pid}|${fullHref}`;
                if (seen.has(dedupeKey)) continue;
                seen.add(dedupeKey);

                refs.push({ id: pid, url: fullHref });
              }

              const orderKey = (id) => {
                if (/^[A-Z]$/.test(id)) return id.charCodeAt(0) - 64;
                if (/^[A-Z]\d+$/.test(id)) return (id.charCodeAt(0) - 64) * 100 + parseInt(id.slice(1), 10);
                return 9999;
              };

              refs.sort((a, b) => orderKey(a.id) - orderKey(b.id));
              return refs;
            }
            """
        )

        normalized_refs: list[dict[str, str]] = []
        for idx, ref in enumerate(problem_refs):
            pid = str(ref.get("id", "")).strip() or chr(ord("A") + idx)
            raw_url = str(ref.get("url", "")).strip()
            full_url = urljoin(contest_url, raw_url) if raw_url else contest_url
            normalized_refs.append({"id": pid, "url": full_url})

        if not normalized_refs:
            raise ScraperError(
                "No problems found on the contest page. "
                "Check URL/auth state or update selectors."
            )

        return {
            "contest_id": contest_id,
            "contest_title": contest_title,
            "problem_refs": normalized_refs,
        }

    async def _scrape_problem(self, problem_ref: dict[str, str]) -> dict[str, Any]:
        if self._context is None:
            raise ScraperError("Browser context is not initialized.")

        page = await self._context.new_page()
        try:
            await self._goto_with_retries(page, problem_ref["url"], wait_until="networkidle")

            extracted = await page.evaluate(
                """
                () => {
                  const clean = (text) => (text || '').replace(/\r/g, '').trim();

                  const pickStatementElement = () => {
                    const selectors = [
                      '#prob-content',
                      '#problem-content',
                      '.problem-content',
                      '.panel-body',
                      '.content',
                      'article'
                    ];

                    const candidates = [];
                    for (const sel of selectors) {
                      document.querySelectorAll(sel).forEach((el) => candidates.push(el));
                    }

                    if (!candidates.length) return document.body;

                    candidates.sort((a, b) => (b.innerText?.length || 0) - (a.innerText?.length || 0));
                    return candidates[0] || document.body;
                  };

                  const findTimeLimit = () => {
                    const bodyText = document.body?.innerText || '';
                    const directMatch = bodyText.match(/Time\s*Limit\s*:?\s*([^\n]+)/i);
                    if (directMatch && directMatch[1]) return clean(directMatch[1]);

                    const labelNodes = Array.from(document.querySelectorAll('td, th, dt, strong, b, span, div'));
                    for (const node of labelNodes) {
                      const label = clean(node.textContent);
                      if (!/time\s*limit/i.test(label)) continue;

                      const siblingText = clean(node.nextElementSibling?.textContent || '');
                      if (siblingText) return siblingText;

                      const parentText = clean(node.parentElement?.textContent || '');
                      if (parentText && parentText.length < 120) return parentText;
                    }

                    return '';
                  };

                  const samplePairsFromKnownSelectors = () => {
                    const inputEls = Array.from(document.querySelectorAll(
                      '.sample-test .input pre, .sample_input pre, .sample-input pre, pre[id*="sample-input"], pre[class*="sample-input"]'
                    ));
                    const outputEls = Array.from(document.querySelectorAll(
                      '.sample-test .output pre, .sample_output pre, .sample-output pre, pre[id*="sample-output"], pre[class*="sample-output"]'
                    ));

                    const pairs = [];
                    const length = Math.min(inputEls.length, outputEls.length);
                    for (let i = 0; i < length; i++) {
                      pairs.push({ in: clean(inputEls[i].textContent), out: clean(outputEls[i].textContent) });
                    }
                    return pairs;
                  };

                  const samplePairsFromHeadings = () => {
                    const pairs = [];
                    const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,strong,b,p,div,span'));

                    const nextPreText = (start) => {
                      let cur = start.nextElementSibling;
                      let depth = 0;
                      while (cur && depth < 6) {
                        const pre = cur.matches('pre,code,textarea') ? cur : cur.querySelector('pre,code,textarea');
                        if (pre) return clean(pre.textContent);
                        cur = cur.nextElementSibling;
                        depth += 1;
                      }
                      return '';
                    };

                    const inputs = [];
                    const outputs = [];

                    for (const h of headings) {
                      const txt = clean(h.textContent).toLowerCase();
                      if (/sample\s*input/.test(txt)) {
                        const val = nextPreText(h);
                        if (val) inputs.push(val);
                      }
                      if (/sample\s*output/.test(txt)) {
                        const val = nextPreText(h);
                        if (val) outputs.push(val);
                      }
                    }

                    const length = Math.min(inputs.length, outputs.length);
                    for (let i = 0; i < length; i++) {
                      pairs.push({ in: inputs[i], out: outputs[i] });
                    }
                    return pairs;
                  };

                  const fallbackPairsFromPre = () => {
                    const preBlocks = Array.from(document.querySelectorAll('pre'))
                      .map((el) => clean(el.textContent))
                      .filter(Boolean);

                    const pairs = [];
                    for (let i = 0; i + 1 < preBlocks.length; i += 2) {
                      pairs.push({ in: preBlocks[i], out: preBlocks[i + 1] });
                    }
                    return pairs;
                  };

                  const statementEl = pickStatementElement();
                  const htmlContent = clean(statementEl?.innerHTML || '');

                  let samples = samplePairsFromKnownSelectors();
                  if (!samples.length) samples = samplePairsFromHeadings();
                  if (!samples.length) samples = fallbackPairsFromPre();

                  return {
                    html_content: htmlContent,
                    time_limit: findTimeLimit(),
                    samples
                  };
                }
                """
            )

            return {
                "id": problem_ref["id"],
                "html_content": extracted.get("html_content", ""),
                "time_limit": extracted.get("time_limit", ""),
                "samples": extracted.get("samples", []),
                "source_url": problem_ref["url"],
            }
        finally:
            await page.close()

    @staticmethod
    def _extract_contest_id(contest_url: str) -> str:
        path = urlparse(contest_url).path
        match = re.search(r"/contest/(\d+)", path)
        if match:
            return match.group(1)

        query_match = re.search(r"[?&](?:cid|contestId)=(\d+)", contest_url, flags=re.IGNORECASE)
        if query_match:
            return query_match.group(1)

        return ""
