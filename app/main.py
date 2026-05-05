from fastapi import FastAPI

from app.api.memory import router as memory_router
from app.api.routes import router
from app.infrastructure.config.settings import settings

app = FastAPI(title=settings.app_name)
app.include_router(router)
app.include_router(memory_router)
