from fastapi import FastAPI

app = FastAPI(
    title="AI Revenue Recovery Agent",
    version="0.1.0",
    description="API for recovering revenue at risk from "
    "failed payments and checkout abandonment",
)


@app.get("/health", tags=["health"])
async def health_check():
    return {"status": "ok", "message": "API is healthy"}
