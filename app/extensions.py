"""Shared Flask extension singletons.

Kept in a dedicated module so models, blueprints and services can import the
extension objects without creating circular imports.
"""

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
