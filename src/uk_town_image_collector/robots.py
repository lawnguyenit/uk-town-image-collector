from __future__ import annotations

from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .transport import HtmlClient, StopRun


class RobotsGuard:
    def __init__(self, client: HtmlClient):
        self.client = client
        self.parsers: dict[str, RobotFileParser] = {}
        self.evidence: list[dict[str, object]] = []

    def preflight(self) -> None:
        for host in ("en.wikipedia.org", "commons.wikimedia.org"):
            url = f"https://{host}/robots.txt"
            response = self.client.fetch(url, purpose="robots", use_cache=False)
            if response.status_code != 200:
                raise StopRun("ROBOTS_UNAVAILABLE", f"Could not verify current robots rules for {host}: HTTP {response.status_code}", response)
            parser = RobotFileParser(url)
            parser.parse(response.text.splitlines())
            self.parsers[host] = parser
            self.evidence.append({"host": host, "url": url, "status": response.status_code, "sha256": response.sha256, "retrieved_at_utc": response.retrieved_at_utc})

    def allows(self, url: str) -> bool:
        host = urlsplit(url).hostname
        parser = self.parsers.get(host or "")
        if parser is None:
            return False
        return parser.can_fetch(self.client.user_agent, url)
