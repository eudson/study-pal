from fastapi import APIRouter, Depends

from config import Settings, get_settings
from schemas.health import HealthStatus

router = APIRouter()


@router.get("/health", response_model=HealthStatus, operation_id="get_health")
def get_health(settings: Settings = Depends(get_settings)) -> HealthStatus:
    return HealthStatus(app_name=settings.app_name, version=settings.app_version)
