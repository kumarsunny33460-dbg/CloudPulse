"""Blueprint registry for the CloudPulse REST API."""

from api import (
    activity,
    applications,
    deployments,
    exports,
    incidents,
    monitoring,
    overview,
    system,
)

BLUEPRINTS = (
    system.blueprint,
    applications.blueprint,
    incidents.blueprint,
    deployments.blueprint,
    monitoring.blueprint,
    overview.blueprint,
    activity.blueprint,
    exports.blueprint,
)


def csrf_exemptions() -> list:
    """Return the blueprints exempt from Flask-WTF form token checks.

    The API is a JSON surface consumed by ``fetch`` with the
    ``X-Requested-With`` header, which the origin guard in ``app.py`` requires.
    A hidden form token would add a round trip without adding protection, so the
    REST layer is exempted and relies on the origin check plus the session
    cookie's ``SameSite`` policy. The browser facing form routes stay protected.
    """
    return list(BLUEPRINTS)


def register(app) -> None:
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)


__all__ = ["register", "BLUEPRINTS", "csrf_exemptions"]
