"""Local example: uvicorn examples.upstream:app --port 8081."""

from fastapi import FastAPI

app = FastAPI()


@app.get("/hello")
async def hello(name: str = "Kite"):
    return {"message": f"Hello, {name}!"}
