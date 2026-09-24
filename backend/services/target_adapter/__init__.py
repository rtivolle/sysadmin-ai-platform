"""
Scoped Target Execution Adapter Package.
Provides secure host service execution and staged atomic configuration deployment.
"""
from .config import (
    ALLOWED_ACTIONS,
    WHITELISTED_SERVICES,
    WHITELISTED_CONFIG_ROOTS,
    FORBIDDEN_TARGET_PATHS,
    normalize_service_name,
    validate_target_service,
    validate_target_config_path,
)
from .service_manager import ServiceManager, get_service_manager
from .config_deployer import ConfigDeployer, get_config_deployer
from .adapter import TargetAdapter, get_target_adapter
from .router import router as target_adapter_router

__all__ = [
    "ALLOWED_ACTIONS",
    "WHITELISTED_SERVICES",
    "WHITELISTED_CONFIG_ROOTS",
    "FORBIDDEN_TARGET_PATHS",
    "normalize_service_name",
    "validate_target_service",
    "validate_target_config_path",
    "ServiceManager",
    "get_service_manager",
    "ConfigDeployer",
    "get_config_deployer",
    "TargetAdapter",
    "get_target_adapter",
    "target_adapter_router",
]
