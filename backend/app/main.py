from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.internal import router as internal_router
from app.api.public import router as public_router
from app.api.webhooks import router as webhooks_router
from app.core.rate_limit import limiter

app = FastAPI(title="StreamClip Co.")
app.state.limiter = limiter
app.add_exception_handler(
    RateLimitExceeded,
    _rate_limit_exceeded_handler,  # type: ignore[arg-type]  # slowapi's canonical handler; narrower than Starlette's Exception-typed stub
)

app.include_router(public_router)
app.include_router(internal_router)
app.include_router(webhooks_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
