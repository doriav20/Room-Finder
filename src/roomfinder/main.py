from fastapi import FastAPI

from roomfinder import __version__

app = FastAPI(title="RoomFinder", version=__version__)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
