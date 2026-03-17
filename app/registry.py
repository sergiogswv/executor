import yaml
import logging
from pathlib import Path
from app.models import ServiceDefinition

logger = logging.getLogger("ejecutor.registry")


class ServiceRegistry:
    """
    Carga y expone el catálogo de servicios desde services.yaml.
    El Ejecutor consulta aquí antes de abrir cualquier terminal.
    """

    def __init__(self, config_path: str = "services.yaml"):
        self._services: dict[str, ServiceDefinition] = {}
        self._path = Path(config_path)
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            logger.warning(f"⚠️  services.yaml no encontrado en {self._path.resolve()}")
            return

        with open(self._path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        services_raw = raw.get("services", {})
        for key, data in services_raw.items():
            self._services[key] = ServiceDefinition(**data)

        logger.info(f"✅ {len(self._services)} servicios cargados: {list(self._services.keys())}")

    def get(self, service_key: str) -> ServiceDefinition | None:
        return self._services.get(service_key)

    def list_all(self) -> dict[str, ServiceDefinition]:
        return dict(self._services)

    def reload(self) -> None:
        """Recarga el archivo sin reiniciar el servidor."""
        self._services.clear()
        self._load()
