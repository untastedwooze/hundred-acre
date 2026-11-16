from fastapi import FastAPI

app = FastAPI(title="hundred-acre")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
