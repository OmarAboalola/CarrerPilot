"""Public Greenhouse GET connector; no arbitrary URL fetching."""

import asyncio
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import Settings
from app.errors import AppError


def job_key(job: dict) -> str:
    """The same application URL can be repeated under different source IDs."""
    url = job.get("url", "")
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.hostname:
        path=parsed.path.rstrip("/")
        requisition=re.search(r"/job/([A-Za-z0-9_-]{3,80})(?:/|$)",path,re.I)
        if requisition:
            path="/job/"+requisition[1]
        link = urlunsplit((parsed.scheme, parsed.netloc.casefold(), path, parsed.query, ""))
        return link + "|" + " ".join(job.get("title", "").casefold().split()) + "|" + " ".join(job.get("company", "").casefold().split())
    return job["id"]


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "li", "br", "div", "h1", "h2", "h3", "h4", "ul", "ol"} and not self.hidden:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "li", "div", "h1", "h2", "h3", "h4", "ul", "ol"} and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str) -> str:
    from html import unescape

    parser = TextParser()
    parser.feed(unescape(value))
    return "\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines() if line.strip())


async def fetch_jobs(settings: Settings) -> tuple[list[dict], list[str]]:
    jobs, errors = [], []
    async with httpx.AsyncClient(timeout=settings.request_timeout, follow_redirects=False) as client:
        for board in settings.greenhouse_boards.split(","):
            url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
            for attempt in range(3):
                try:
                    async with client.stream("GET", url) as response:
                        if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                            await asyncio.sleep(0.25 * (2**attempt))
                            continue
                        if response.status_code != 200:
                            raise AppError(
                                "source_failed",
                                f"Greenhouse board {board} returned status {response.status_code}.",
                                502,
                            )
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 5_000_000:
                                raise AppError(
                                    "source_too_large", "Job source exceeded response-size limit.", 502
                                )
                            chunks.append(chunk)
                    import json

                    payload = json.loads(b"".join(chunks))
                    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                        raise ValueError("Missing jobs array")
                    now = datetime.now(UTC).isoformat()
                    for item in payload["jobs"]:
                        if (not isinstance(item, dict) or not isinstance(item.get("id"), (str, int))
                            or not all(isinstance(item.get(key), str) and item[key].strip() for key in ["title", "absolute_url"])
                            or not isinstance(item.get("content", ""), str)
                            or not isinstance(item.get("location", {}), dict)):
                            errors.append(f"Greenhouse board {board}: skipped an invalid listing; valid results retained.")
                            continue
                        jobs.append(
                            {
                                "id": f"{board}:{item['id']}",
                                "title": item["title"][:250],
                                "company": board,
                                "location": item.get("location", {}).get("name", "Unknown"),
                                "description": plain_text(item.get("content", ""))[:25000],
                                "url": item["absolute_url"],
                                "retrieved_at": now,
                                "mode": "live",
                                "source": url,
                                "updated_at": item.get("updated_at"),
                            }
                        )
                    break
                except (httpx.HTTPError, ValueError, AppError) as error:
                    if attempt < 2 and isinstance(error, httpx.HTTPError):
                        await asyncio.sleep(0.25 * (2**attempt))
                        continue
                    errors.append(f"Greenhouse board {board}: unavailable or invalid response.")
                    break
    unique = {job_key(job): job for job in jobs}
    return list(unique.values()), list(dict.fromkeys(errors))
