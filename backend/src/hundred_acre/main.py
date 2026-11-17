from fastapi import FastAPI

from hundred_acre.spend.api import router as spend_router

app = FastAPI(title="hundred-acre")
app.include_router(spend_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
