from flask import Blueprint

bp = Blueprint("forums", __name__)

from . import routes  # noqa: E402,F401
