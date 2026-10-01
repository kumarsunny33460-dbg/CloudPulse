"""Session guards for CloudPulse.

These two decorators are the original CloudPulse access control helpers. The
implementation is unchanged; they simply live in their own module so both the
HTML routes and the new REST blueprints can share them without a circular
import through ``app``.
"""

from functools import wraps

from flask import jsonify, redirect, request, session, url_for


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
