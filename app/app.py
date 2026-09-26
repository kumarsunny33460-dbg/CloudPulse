from flask import (
    Flask,
    jsonify,
    request,
    render_template,
    redirect,
    url_for,
    session
)

from models import db, User, Application, Incident, Deployment

from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
import os
import logging
from functools import wraps

import time


app = Flask(__name__)
# ============================================================
# APPLICATION LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("cloudpulse")


# ============================================================
# APPLICATION CONFIGURATION
# ============================================================
database_url = os.getenv("DATABASE_URL")

if database_url:
# Some PostgreSQL providers may return postgres://.
# SQLAlchemy expects postgresql://.
    if database_url.startswith("postgres://"):
        database_url = database_url.replace(
        "postgres://",
        "postgresql://",
     1
    )
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url

else:
 app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///cloudpulse.db"

 app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY") or "cloudpulse-development-secret-key"

db.init_app(app)


with app.app_context():
    db.create_all()


# ============================================================
# AUTHENTICATION HELPERS
# ============================================================

def user_to_dict(user):

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "created_at": (
            user.created_at.isoformat()
            if user.created_at
            else None
        )
    }


def login_required(function):

    @wraps(function)
    def decorated_function(*args, **kwargs):

        if "user_id" not in session:

            if request.path.startswith("/api/"):
                return jsonify({
                    "error": "Authentication required"
                }), 401

            return redirect(url_for("login"))

        return function(*args, **kwargs)

    return decorated_function

# ============================================================
# ADMIN ACCESS CONTROL
# ============================================================

def admin_required(function):

    @wraps(function)
    def decorated_function(*args, **kwargs):

        if "user_id" not in session:

            return jsonify({
                "error": "Authentication required"
            }), 401

        if session.get("role") != "Admin":

            return jsonify({
                "error": "Admin access required"
            }), 403

        return function(*args, **kwargs)

    return decorated_function


# ============================================================
# APPLICATION HELPER
# ============================================================

def application_to_dict(application):

    return {
        "id": application.id,
        "name": application.name,
        "url": application.url,
        "environment": application.environment,
        "status": application.status,
        "description": application.description,
        "created_at": (
            application.created_at.isoformat()
            if application.created_at
            else None
        )
    }


# ============================================================
# INCIDENT HELPER
# ============================================================

def incident_to_dict(incident):

    return {
        "id": incident.id,
        "application_id": incident.application_id,
        "application_name": (
            incident.application.name
            if incident.application
            else None
        ),
        "title": incident.title,
        "description": incident.description,
        "severity": incident.severity,
        "status": incident.status,
        "created_at": (
            incident.created_at.isoformat()
            if incident.created_at
            else None
        ),
        "resolved_at": (
            incident.resolved_at.isoformat()
            if incident.resolved_at
            else None
        )
    }


# ============================================================
# DEPLOYMENT HELPER
# ============================================================

def deployment_to_dict(deployment):

    return {
        "id": deployment.id,
        "application_id": deployment.application_id,
        "application_name": (
            deployment.application.name
            if deployment.application
            else None
        ),
        "version": deployment.version,
        "environment": deployment.environment,
        "status": deployment.status,
        "deployed_at": (
            deployment.deployed_at.isoformat()
            if deployment.deployed_at
            else None
        )
    }


# ============================================================
# REGISTER
# ============================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if "user_id" in session:
        return redirect(url_for("home"))

    if request.method == "GET":
        return render_template("register.html")

    data = request.form

    username = data.get("username", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not username or not email or not password:

        return render_template(
            "register.html",
            error="All fields are required."
        )

    if len(password) < 6:

        return render_template(
            "register.html",
            error="Password must contain at least 6 characters."
        )

    existing_username = User.query.filter_by(
        username=username
    ).first()

    if existing_username:

        return render_template(
            "register.html",
            error="Username already exists."
        )

    existing_email = User.query.filter_by(
        email=email
    ).first()

    if existing_email:

        return render_template(
            "register.html",
            error="Email is already registered."
        )

    user_count = User.query.count()

    role = "Admin" if user_count == 0 else "Viewer"

    user = User(
        username=username,
        email=email,
        password_hash=generate_password_hash(password),
        role=role
    )

    db.session.add(user)
    db.session.commit()

    return redirect(url_for("login"))


# ============================================================
# LOGIN
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if "user_id" in session:
        return redirect(url_for("home"))

    if request.method == "GET":
        return render_template("login.html")

    data = request.form

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:

        return render_template(
            "login.html",
            error="Email and password are required."
        )

    user = User.query.filter_by(
        email=email
    ).first()

    if not user:

        return render_template(
            "login.html",
            error="Invalid email or password."
        )

    if not check_password_hash(
        user.password_hash,
        password
    ):

        return render_template(
            "login.html",
            error="Invalid email or password."
        )

    session.clear()

    session["user_id"] = user.id
    session["username"] = user.username
    session["role"] = user.role

    return redirect(url_for("home"))


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("login"))


# ============================================================
# CURRENT USER API
# ============================================================

@app.get("/api/me")
@login_required
def current_user():

    user = db.session.get(
        User,
        session["user_id"]
    )

    if not user:

        session.clear()

        return jsonify({
            "error": "User not found"
        }), 401

    return jsonify({
        "user": user_to_dict(user)
    })


# ============================================================
# HOMEPAGE
# ============================================================

@app.route("/")
@login_required
def home():

    return render_template("index.html")


# ============================================================
# CLOUDPULSE HEALTH CHECK
# ============================================================

@app.route("/health")
def health():

    logger.info("Health endpoint accessed")

    return jsonify({
        "status": "healthy"
    })


@app.route("/api/cicd")
def cicd():

    logger.info("CI/CD verification endpoint accessed")

    return jsonify({
        "status": "success",
        "message": "CloudPulse CI/CD pipeline is working"
    })

# ============================================================
# GET APPLICATIONS
# ============================================================

@app.get("/api/applications")
@login_required
def get_applications():

    applications = Application.query.order_by(
        Application.id.desc()
    ).all()

    result = []

    for application in applications:

        result.append(
            application_to_dict(application)
        )

    return jsonify(result)


# ============================================================
# CREATE APPLICATION
# ============================================================

@app.post("/api/applications")
@admin_required
def create_application():

    data = request.get_json()

    if not data:

        return jsonify({
            "error": "Request body is required"
        }), 400

    name = data.get("name")
    url = data.get("url")
    environment = data.get("environment")
    status = data.get("status", "Active")
    description = data.get("description", "")

    if not name or not url or not environment:

        return jsonify({
            "error": "Name, URL and environment are required"
        }), 400

    application = Application(
        name=name,
        url=url,
        environment=environment,
        status=status,
        description=description
    )

    db.session.add(application)
    db.session.commit()

    return jsonify({
        "message": "Application created successfully",
        "application": application_to_dict(application)
    }), 201


# ============================================================
# UPDATE APPLICATION
# ============================================================

@app.put("/api/applications/<int:application_id>")
@admin_required
def update_application(application_id):

    application = db.session.get(
        Application,
        application_id
    )

    if not application:

        return jsonify({
            "error": "Application not found"
        }), 404

    data = request.get_json()

    if not data:

        return jsonify({
            "error": "Request body is required"
        }), 400

    application.name = data.get(
        "name",
        application.name
    )

    application.url = data.get(
        "url",
        application.url
    )

    application.environment = data.get(
        "environment",
        application.environment
    )

    application.status = data.get(
        "status",
        application.status
    )

    application.description = data.get(
        "description",
        application.description
    )

    db.session.commit()

    return jsonify({
        "message": "Application updated successfully",
        "application": application_to_dict(application)
    })


# ============================================================
# DELETE APPLICATION
# ============================================================

@app.delete("/api/applications/<int:application_id>")
@admin_required
def delete_application(application_id):

    application = db.session.get(
        Application,
        application_id
    )

    if not application:

        return jsonify({
            "error": "Application not found"
        }), 404

    db.session.delete(application)
    db.session.commit()

    return jsonify({
        "message": "Application deleted successfully"
    })


# ============================================================
# SINGLE APPLICATION HEALTH CHECK
# ============================================================

@app.get("/api/applications/<int:application_id>/health")
@login_required
def application_health(application_id):

    application = db.session.get(
        Application,
        application_id
    )

    if not application:

        return jsonify({
            "error": "Application not found"
        }), 404

    result = check_application_health(application)

    incident = create_incident_if_needed(
        application,
        result
    )

    response_data = dict(result)

    response_data["incident_created"] = (
        incident is not None
    )

    if incident:

        response_data["incident_id"] = incident.id

    return jsonify(response_data)


# ============================================================
# MONITOR ALL APPLICATIONS
# ============================================================

@app.get("/api/monitor")
@login_required
def monitor_all_applications():

    logger.info("Application monitoring started")

    applications = Application.query.order_by(
        Application.id.desc()
    ).all()

    logger.info(
        "Monitoring %s registered applications",
        len(applications)
    )

    results = []

    for application in applications:

        logger.info(
            "Checking application health: %s",
            application.name
        )

        result = check_application_health(application)

        logger.info(
            "Health check result: %s -> %s",
            application.name,
            result.get("health")
        )

        create_incident_if_needed(
            application,
            result
        )

        results.append(result)

    total_applications = len(results)

    healthy_applications = sum(
        1
        for result in results
        if result["health"] == "UP"
    )

    unhealthy_applications = sum(
        1
        for result in results
        if result["health"] == "DOWN"
    )

    open_incidents = Incident.query.filter_by(
        status="Open"
    ).count()

    logger.info(
        "Monitoring completed | Total: %s | Healthy: %s | Unhealthy: %s | Open incidents: %s",
        total_applications,
        healthy_applications,
        unhealthy_applications,
        open_incidents
    )

    return jsonify({
        "summary": {
            "total": total_applications,
            "healthy": healthy_applications,
            "unhealthy": unhealthy_applications,
            "open_incidents": open_incidents
        },
        "applications": results
    })

# ============================================================
# GET ALL INCIDENTS
# ============================================================

@app.get("/api/incidents")
@login_required
def get_incidents():

    incidents = Incident.query.order_by(
        Incident.id.desc()
    ).all()

    result = []

    for incident in incidents:

        result.append(
            incident_to_dict(incident)
        )

    return jsonify(result)


# ============================================================
# GET SINGLE INCIDENT
# ============================================================

@app.get("/api/incidents/<int:incident_id>")
@login_required
def get_incident(incident_id):

    incident = db.session.get(
        Incident,
        incident_id
    )

    if not incident:

        return jsonify({
            "error": "Incident not found"
        }), 404

    return jsonify(
        incident_to_dict(incident)
    )


# ============================================================
# CREATE INCIDENT MANUALLY
# ============================================================

@app.post("/api/incidents")
@login_required
def create_incident():

    data = request.get_json()

    if not data:

        return jsonify({
            "error": "Request body is required"
        }), 400

    application_id = data.get("application_id")
    title = data.get("title")
    description = data.get("description", "")
    severity = data.get("severity", "Medium")
    status = data.get("status", "Open")

    if not application_id or not title:

        return jsonify({
            "error": "Application ID and title are required"
        }), 400

    application = db.session.get(
        Application,
        application_id
    )

    if not application:

        return jsonify({
            "error": "Application not found"
        }), 404

    if severity not in (
        "Low",
        "Medium",
        "High",
        "Critical"
    ):

        return jsonify({
            "error": "Invalid severity"
        }), 400

    if status not in (
        "Open",
        "Resolved"
    ):

        return jsonify({
            "error": "Invalid incident status"
        }), 400

    incident = Incident(
        application_id=application_id,
        title=title,
        description=description,
        severity=severity,
        status=status
    )

    if status == "Resolved":

        incident.resolved_at = datetime.utcnow()

    db.session.add(incident)
    db.session.commit()

    return jsonify({
        "message": "Incident created successfully",
        "incident": incident_to_dict(incident)
    }), 201


# ============================================================
# UPDATE INCIDENT
# ============================================================

@app.put("/api/incidents/<int:incident_id>")
@login_required
def update_incident(incident_id):

    incident = db.session.get(
        Incident,
        incident_id
    )

    if not incident:

        return jsonify({
            "error": "Incident not found"
        }), 404

    data = request.get_json()

    if not data:

        return jsonify({
            "error": "Request body is required"
        }), 400

    if "title" in data:

        incident.title = data["title"]

    if "description" in data:

        incident.description = data["description"]

    if "severity" in data:

        if data["severity"] not in (
            "Low",
            "Medium",
            "High",
            "Critical"
        ):

            return jsonify({
                "error": "Invalid severity"
            }), 400

        incident.severity = data["severity"]

    if "status" in data:

        if data["status"] not in (
            "Open",
            "Resolved"
        ):

            return jsonify({
                "error": "Invalid incident status"
            }), 400

        incident.status = data["status"]

        if data["status"] == "Resolved":

            if not incident.resolved_at:

                incident.resolved_at = datetime.utcnow()

        else:

            incident.resolved_at = None

    db.session.commit()

    return jsonify({
        "message": "Incident updated successfully",
        "incident": incident_to_dict(incident)
    })


# ============================================================
# RESOLVE INCIDENT
# ============================================================

@app.put("/api/incidents/<int:incident_id>/resolve")
@login_required
def resolve_incident(incident_id):

    incident = db.session.get(
        Incident,
        incident_id
    )

    if not incident:

        logger.warning(
            "Incident resolution failed | Incident ID: %s | Incident not found",
            incident_id
        )

        return jsonify({
            "error": "Incident not found"
        }), 404

    incident.status = "Resolved"
    incident.resolved_at = datetime.utcnow()

    db.session.commit()

    logger.info(
        "Incident resolved | Incident ID: %s | Application ID: %s",
        incident.id,
        incident.application_id
    )

    return jsonify({
        "message": "Incident resolved successfully",
        "incident": incident_to_dict(incident)
    })
# ============================================================
# DELETE INCIDENT
# ============================================================

@app.delete("/api/incidents/<int:incident_id>")
@login_required
def delete_incident(incident_id):

    incident = db.session.get(
        Incident,
        incident_id
    )

    if not incident:

        return jsonify({
            "error": "Incident not found"
        }), 404

    db.session.delete(incident)
    db.session.commit()

    return jsonify({
        "message": "Incident deleted successfully"
    })


# ============================================================
# GET ALL DEPLOYMENTS
# ============================================================

@app.get("/api/deployments")
@login_required
def get_deployments():

    deployments = Deployment.query.order_by(
        Deployment.id.desc()
    ).all()

    result = []

    for deployment in deployments:

        result.append(
            deployment_to_dict(deployment)
        )

    return jsonify(result)


# ============================================================
# GET SINGLE DEPLOYMENT
# ============================================================

@app.get("/api/deployments/<int:deployment_id>")
@login_required
def get_deployment(deployment_id):

    deployment = db.session.get(
        Deployment,
        deployment_id
    )

    if not deployment:

        return jsonify({
            "error": "Deployment not found"
        }), 404

    return jsonify(
        deployment_to_dict(deployment)
    )


# ============================================================
# CREATE DEPLOYMENT
# ============================================================

@app.post("/api/deployments")
@login_required
def create_deployment():

    data = request.get_json()

    if not data:

        return jsonify({
            "error": "Request body is required"
        }), 400

    application_id = data.get("application_id")
    version = data.get("version")
    environment = data.get("environment")
    status = data.get("status", "Successful")

    if not application_id or not version or not environment:

        return jsonify({
            "error": (
                "Application ID, version and environment "
                "are required"
            )
        }), 400

    application = db.session.get(
        Application,
        application_id
    )

    if not application:

        return jsonify({
            "error": "Application not found"
        }), 404

    allowed_statuses = (
        "Successful",
        "Failed",
        "In Progress",
        "Rolled Back"
    )

    if status not in allowed_statuses:

        return jsonify({
            "error": "Invalid deployment status"
        }), 400

    deployment = Deployment(
        application_id=application_id,
        version=version,
        environment=environment,
        status=status
    )

    db.session.add(deployment)
    db.session.commit()

    return jsonify({
        "message": "Deployment created successfully",
        "deployment": deployment_to_dict(deployment)
    }), 201


# ============================================================
# UPDATE DEPLOYMENT
# ============================================================

@app.put("/api/deployments/<int:deployment_id>")
@login_required
def update_deployment(deployment_id):

    deployment = db.session.get(
        Deployment,
        deployment_id
    )

    if not deployment:

        return jsonify({
            "error": "Deployment not found"
        }), 404

    data = request.get_json()

    if not data:

        return jsonify({
            "error": "Request body is required"
        }), 400

    if "application_id" in data:

        application = db.session.get(
            Application,
            data["application_id"]
        )

        if not application:

            return jsonify({
                "error": "Application not found"
            }), 404

        deployment.application_id = data["application_id"]

    if "version" in data:

        if not data["version"]:

            return jsonify({
                "error": "Version cannot be empty"
            }), 400

        deployment.version = data["version"]

    if "environment" in data:

        if not data["environment"]:

            return jsonify({
                "error": "Environment cannot be empty"
            }), 400

        deployment.environment = data["environment"]

    if "status" in data:

        allowed_statuses = (
            "Successful",
            "Failed",
            "In Progress",
            "Rolled Back"
        )

        if data["status"] not in allowed_statuses:

            return jsonify({
                "error": "Invalid deployment status"
            }), 400

        deployment.status = data["status"]

    db.session.commit()

    return jsonify({
        "message": "Deployment updated successfully",
        "deployment": deployment_to_dict(deployment)
    })


# ============================================================
# DELETE DEPLOYMENT
# ============================================================

@app.delete("/api/deployments/<int:deployment_id>")
@login_required
def delete_deployment(deployment_id):

    deployment = db.session.get(
        Deployment,
        deployment_id
    )

    if not deployment:

        return jsonify({
            "error": "Deployment not found"
        }), 404

    db.session.delete(deployment)
    db.session.commit()

    return jsonify({
        "message": "Deployment deleted successfully"
    })


# ============================================================
# AUTOMATIC INCIDENT CREATION
# ============================================================

def create_incident_if_needed(
    application,
    health_result
):

    if health_result["health"] != "DOWN":

        logger.info(
            "No incident required | Application: %s | Health: %s",
            application.name,
            health_result["health"]
        )

        return None

    logger.warning(
        "Application DOWN | Checking for existing incident | Application: %s",
        application.name
    )

    existing_incident = Incident.query.filter_by(
        application_id=application.id,
        status="Open"
    ).first()

    if existing_incident:

        logger.info(
            "Existing open incident found | Application: %s | Incident ID: %s",
            application.name,
            existing_incident.id
        )

        return existing_incident

    incident = Incident(
        application_id=application.id,
        title=f"{application.name} is Down",
        description=(
            f"CloudPulse detected that {application.name} "
            f"is currently unreachable."
        ),
        severity="High",
        status="Open"   
    )

    db.session.add(incident)
    db.session.commit()

    logger.warning(
        "New incident created | Application: %s | Incident ID: %s | Severity: High",
        application.name,
        incident.id
    )

    return incident

# ============================================================
# HEALTH CHECK
# ============================================================

def check_application_health(application):

    logger.info(
        "Starting health check | Application: %s | URL: %s",
        application.name,
        application.url
    )

    parsed_url = urlparse(application.url)

    if parsed_url.scheme not in (
        "http",
        "https"
    ) or not parsed_url.netloc:

        logger.warning(
            "Invalid application URL | Application: %s | URL: %s",
            application.name,
            application.url
        )

        return {
            "application_id": application.id,
            "name": application.name,
            "url": application.url,
            "environment": application.environment,
            "health": "DOWN",
            "response_time_ms": None,
            "http_status": None,
            "message": "Invalid application URL"
        }

    start_time = time.perf_counter()

    try:

        request_object = Request(
            application.url,
            headers={
                "User-Agent": "CloudPulse-Monitor/1.0"
            }
        )

        with urlopen(
            request_object,
            timeout=5
        ) as response:

            elapsed_time = (
                time.perf_counter() -
                start_time
            ) * 1000

            response_time = round(
                elapsed_time,
                2
            )

            logger.info(
                "Health check successful | Application: %s | Status: %s | Response time: %sms",
                application.name,
                response.status,
                response_time
            )

            return {
                "application_id": application.id,
                "name": application.name,
                "url": application.url,
                "environment": application.environment,
                "health": "UP",
                "response_time_ms": response_time,
                "http_status": response.status,
                "message": "Application is reachable"
            }

    except HTTPError as error:

        elapsed_time = (
            time.perf_counter() -
            start_time
        ) * 1000

        response_time = round(
            elapsed_time,
            2
        )

        logger.warning(
            "Application returned HTTP error | Application: %s | Status: %s | Response time: %sms",
            application.name,
            error.code,
            response_time
        )

        return {
            "application_id": application.id,
            "name": application.name,
            "url": application.url,
            "environment": application.environment,
            "health": "UP",
            "response_time_ms": response_time,
            "http_status": error.code,
            "message": (
                "Application responded with "
                "an HTTP error"
            )
        }

    except (
        URLError,
        TimeoutError
    ):

        logger.error(
            "Application unreachable | Application: %s | URL: %s",
            application.name,
            application.url
        )

        return {
            "application_id": application.id,
            "name": application.name,
            "url": application.url,
            "environment": application.environment,
            "health": "DOWN",
            "response_time_ms": None,
            "http_status": None,
            "message": "Application is unreachable"
        }

    except Exception as error:

        logger.exception(
            "Unexpected health check error | Application: %s",
            application.name
        )

        return {
            "application_id": application.id,
            "name": application.name,
            "url": application.url,
            "environment": application.environment,
            "health": "DOWN",
            "response_time_ms": None,
            "http_status": None,
            "message": "Health check failed"
        }


# ============================================================
# RUN APPLICATION
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )