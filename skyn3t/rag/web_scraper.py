"""Web scraper for RAG knowledge ingestion."""

import re
from typing import Any, Dict, List
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx


class WebScraper:
    """Scrape web pages and extract clean text for RAG."""

    DEFAULT_UA = "skyn3t-scraper/0.1"

    def __init__(
        self,
        timeout: int = 30,
        max_depth: int = 2,
        respect_robots: bool = True,
        user_agent: str = DEFAULT_UA,
    ):
        self.timeout = timeout
        self.max_depth = max_depth
        self.respect_robots = respect_robots
        self.user_agent = user_agent
        self._visited: set = set()
        # Cache parsed RobotFileParser instances per host so each host's
        # robots.txt is fetched at most once per scraper.
        self._robots_parsers: Dict[str, robotparser.RobotFileParser] = {}

    async def scrape_url(
        self, url: str, depth: int = 0
    ) -> Dict[str, Any]:
        """Scrape a single URL."""
        if depth > self.max_depth or url in self._visited:
            return {"url": url, "content": "", "links": []}

        self._visited.add(url)

        if self.respect_robots and not await self._can_fetch(url):
            return {"url": url, "content": "", "links": [], "blocked": True}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                headers = {
                    "User-Agent": "SkyN3t-Orchestrator/1.0 (Research Bot)"
                }
                response = await client.get(url, headers=headers, follow_redirects=True)
                response.raise_for_status()

                content_type = response.headers.get("content-type", "")
                if "text/html" not in content_type:
                    return {
                        "url": url,
                        "content": response.text[:5000],
                        "links": [],
                    }

                text = self._extract_text(response.text, url)
                links = self._extract_links(response.text, url)

                return {
                    "url": url,
                    "title": self._extract_title(response.text),
                    "content": text,
                    "links": links,
                    "depth": depth,
                }

        except Exception as e:
            return {
                "url": url,
                "content": "",
                "links": [],
                "error": str(e),
            }

    async def scrape_recursive(
        self, url: str, max_pages: int = 10
    ) -> List[Dict[str, Any]]:
        """Scrape a URL and follow links recursively."""
        results: List[Dict[str, Any]] = []
        to_visit = [(url, 0)]

        while to_visit and len(results) < max_pages:
            current_url, depth = to_visit.pop(0)
            result = await self.scrape_url(current_url, depth)
            results.append(result)

            if depth < self.max_depth:
                for link in result.get("links", [])[:5]:
                    if link not in self._visited:
                        to_visit.append((link, depth + 1))

        return results

    def _extract_text(self, html: str, base_url: str) -> str:
        """Extract clean text from HTML."""
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")

            # Remove script and style elements
            for script in soup(["script", "style", "nav", "footer", "header"]):
                script.decompose()

            # Get text
            text = soup.get_text(separator="\n")

            # Clean up
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = "\n".join(chunk for chunk in chunks if chunk)

            return text[:50000]  # Limit size
        except ImportError:
            # Fallback regex extraction
            text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:50000]

    def _extract_links(self, html: str, base_url: str) -> List[str]:
        """Extract links from HTML."""
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")
            links: List[str] = []
            for a in soup.find_all("a", href=True):
                href_raw = a.get("href")
                if not isinstance(href_raw, str):
                    continue
                href = urljoin(base_url, href_raw)
                if urlparse(href).netloc == urlparse(base_url).netloc:
                    links.append(href)
            return links[:20]
        except ImportError:
            # Fallback regex
            links = re.findall(r'href=["\'](.*?)["\']', html)
            return [
                urljoin(base_url, link)
                for link in links
                if link.startswith("http") or link.startswith("/")
            ][:20]

    def _extract_title(self, html: str) -> str:
        """Extract page title."""
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if match:
            return re.sub(r"\s+", " ", match.group(1).strip())
        return ""

    async def _can_fetch(self, url: str) -> bool:
        """Check robots.txt using urllib.robotparser scoped to our UA.

        Conservative: on fetch failure we allow the fetch (matches the previous
        behavior). On a 4xx other than 404, also allow (host has no robots.txt
        policy). 404 → no robots.txt → allow. 200 → respect the policy.
        """
        parsed = urlparse(url)
        host_key = f"{parsed.scheme}://{parsed.netloc}"
        rp = self._robots_parsers.get(host_key)
        if rp is None:
            rp = robotparser.RobotFileParser()
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get(f"{host_key}/robots.txt")
                    if response.status_code == 200:
                        rp.parse(response.text.splitlines())
                    else:
                        # Treat any non-200 as "no policy" → allow.
                        rp.parse([])
            except Exception:
                rp.parse([])
            self._robots_parsers[host_key] = rp
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:
            return True
