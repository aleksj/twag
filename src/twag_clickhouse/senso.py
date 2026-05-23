from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_SENSO_BASE_URL = "https://sdk.senso.ai/api/v1"


@dataclass(frozen=True)
class SensoConfig:
    api_key: str
    base_url: str = DEFAULT_SENSO_BASE_URL
    max_results: int = 5

    @classmethod
    def from_env(cls) -> "SensoConfig | None":
        api_key = os.getenv("SENSO_API_KEY", "").strip()
        if not api_key:
            return None

        return cls(
            api_key=api_key,
            base_url=os.getenv("SENSO_BASE_URL", DEFAULT_SENSO_BASE_URL).strip()
            or DEFAULT_SENSO_BASE_URL,
            max_results=int(os.getenv("SENSO_MAX_RESULTS", "5")),
        )


class SensoService:
    def __init__(self, config: SensoConfig):
        self.config = config

    def search(self, query: str, *, max_results: int | None = None) -> dict[str, Any]:
        body = {
            "query": query,
            "max_results": max_results or self.config.max_results,
        }
        request = urllib.request.Request(
            f"{self.config.base_url.rstrip('/')}/search",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "X-API-Key": self.config.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Senso API error {exc.code}: {detail}") from exc


def format_senso_answer(result: dict[str, Any]) -> str:
    answer = str(result.get("answer") or "").strip()
    if not answer:
        return ""

    sources = []
    seen = set()
    for item in result.get("results") or []:
        title = str(item.get("title") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        score = item.get("score")
        if isinstance(score, int | float):
            sources.append(f"{title} ({score:.2f})")
        else:
            sources.append(title)
        if len(sources) >= 3:
            break

    if sources:
        return f"{answer}\n\nSources: {', '.join(sources)}"
    return answer
