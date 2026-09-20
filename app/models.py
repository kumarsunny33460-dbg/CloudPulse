from datetime import datetime

from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


# ============================================================
# USER MODEL
# ============================================================

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(
        db.String(100),
        nullable=False,
        unique=True
    )

    email = db.Column(
        db.String(150),
        nullable=False,
        unique=True
    )

    password_hash = db.Column(
        db.String(255),
        nullable=False
    )

    role = db.Column(
        db.String(50),
        nullable=False,
        default="Viewer"
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )


# ============================================================
# APPLICATION MODEL
# ============================================================

class Application(db.Model):
    __tablename__ = "applications"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(
        db.String(120),
        nullable=False
    )

    url = db.Column(
        db.String(500),
        nullable=False
    )

    environment = db.Column(
        db.String(50),
        nullable=False,
        default="Development"
    )

    status = db.Column(
        db.String(50),
        nullable=False,
        default="Operational"
    )

    description = db.Column(
        db.Text,
        nullable=True
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    incidents = db.relationship(
        "Incident",
        backref="application",
        lazy=True,
        cascade="all, delete-orphan"
    )

    deployments = db.relationship(
        "Deployment",
        backref="application",
        lazy=True,
        cascade="all, delete-orphan"
    )


# ============================================================
# INCIDENT MODEL
# ============================================================

class Incident(db.Model):
    __tablename__ = "incidents"

    id = db.Column(db.Integer, primary_key=True)

    application_id = db.Column(
        db.Integer,
        db.ForeignKey("applications.id"),
        nullable=False
    )

    title = db.Column(
        db.String(200),
        nullable=False
    )

    description = db.Column(
        db.Text,
        nullable=True
    )

    severity = db.Column(
        db.String(50),
        nullable=False,
        default="Medium"
    )

    status = db.Column(
        db.String(50),
        nullable=False,
        default="Open"
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    resolved_at = db.Column(
        db.DateTime,
        nullable=True
    )


# ============================================================
# DEPLOYMENT MODEL
# ============================================================

class Deployment(db.Model):
    __tablename__ = "deployments"

    id = db.Column(db.Integer, primary_key=True)

    application_id = db.Column(
        db.Integer,
        db.ForeignKey("applications.id"),
        nullable=False
    )

    version = db.Column(
        db.String(100),
        nullable=False
    )

    environment = db.Column(
        db.String(50),
        nullable=False
    )

    status = db.Column(
        db.String(50),
        nullable=False,
        default="Successful"
    )

    deployed_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )
    