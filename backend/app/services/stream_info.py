from dataclasses import dataclass
from datetime import datetime


@dataclass
class StreamInfo:
    external_stream_id: str
    title: str | None
    category: str | None
    viewer_count: int
    started_at: datetime
