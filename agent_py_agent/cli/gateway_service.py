

from __future__ import annotations

from ._gateway_service_handlers import (
    install_service,
    install_systemd,
    is_linux,
    is_macos,
    supports_systemd_services,
    uninstall_service,
    uninstall_systemd,
)

# Backwards compatibility: expose PROJECT_ROOT
from ._gateway_service_unit_gen import (
    PROJECT_ROOT,
    generate_launchd_plist_text,
    generate_systemd_unit_text,
    get_launchd_plist_path,
    get_service_name,
    get_systemd_unit_path,
    is_windows,
)

__all__ = [
    # Unit generation
    "generate_systemd_unit_text",
    "generate_launchd_plist_text",
    "get_service_name",
    "get_systemd_unit_path",
    "get_launchd_plist_path",
    "PROJECT_ROOT",
    # Platform detection
    "is_linux",
    "is_macos",
    "is_windows",
    "supports_systemd_services",
    # Install/Uninstall
    "install_service",
    "install_systemd",
    "uninstall_service",
    "uninstall_systemd",
]