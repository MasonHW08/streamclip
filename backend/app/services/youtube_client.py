from datetime import datetime
from typing import Protocol

import httpx

from app.services.stream_info import StreamInfo

_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


class YouTubeClient(Protocol):
    def get_stream_status(self, channel_id: str) -> StreamInfo | None: ...


class FakeYouTubeClient:
    def __init__(self) -> None:
        self.stream_status: dict[str, StreamInfo] = {}

    def get_stream_status(self, channel_id: str) -> StreamInfo | None:
        return self.stream_status.get(channel_id)


class YouTubeAPIClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def get_stream_status(self, channel_id: str) -> StreamInfo | None:
        # The API key goes in the X-goog-api-key header, NOT the `key` query
        # parameter. httpx embeds the full request URL — query string included —
        # in the HTTPStatusError raised by raise_for_status(), and the poll
        # loop logs that exception with logger.exception(). With the key in the
        # URL, every failed request (403 quotaExceeded is the expected steady
        # state at the shipped poll cadence) would write the live API key into
        # application logs. Google's APIs accept the header form, which keeps
        # the key out of the URL entirely — and so out of exception messages,
        # access logs, and anywhere else the URL surfaces.
        response = httpx.get(
            _SEARCH_URL,
            params={
                "part": "snippet",
                "channelId": channel_id,
                "eventType": "live",
                "type": "video",
            },
            headers={"X-goog-api-key": self._api_key},
            timeout=10.0,
        )
        response.raise_for_status()
        items = response.json()["items"]
        if not items:
            return None
        item = items[0]
        # search.list doesn't return viewer count; a follow-up videos.list call
        # (part=liveStreamingDetails) would be needed for that — out of scope
        # for this task, viewer_count defaults to 0 for YouTube for now.
        return StreamInfo(
            external_stream_id=item["id"]["videoId"],
            title=item["snippet"].get("title") or None,
            category=None,
            viewer_count=0,
            started_at=datetime.fromisoformat(
                item["snippet"]["publishTime"].replace("Z", "+00:00")
            ),
        )
