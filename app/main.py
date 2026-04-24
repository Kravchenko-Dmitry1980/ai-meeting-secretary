from fastapi import FastAPI

from app.api.routes import router
from app.infrastructure.config.settings import settings

app = FastAPI(title=settings.app_name)
app.include_router(router)
