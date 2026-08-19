"""
Persistence layer — one row per pilot, everything else scoped off it.

Legs stay a loose dict the rest of the app already knows how to merge
against DEFAULT_LEG (server.py) rather than ~40 rigid columns: `Leg.data`
holds that dict verbatim, with `flight_number`/`dep_date`/`created_at` as
their own indexed columns purely because the existing dedupe/archive
queries need to filter and order on them.
"""
from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def _now():
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    # Replaces the old fos_simbrief_user localStorage key so it follows the
    # pilot across devices instead of being stuck on one browser.
    default_simbrief_user = db.Column(db.String(64))
    # Replaces fos_last_leg — which leg this pilot was last on, so "Current
    # Flight" / "Request New Data" on Home can be computed server-side.
    current_leg_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Leg(db.Model):
    __tablename__ = "legs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    flight_number = db.Column(db.String(16))
    dep_date = db.Column(db.String(16))
    created_at = db.Column(db.DateTime(timezone=True), default=_now, index=True)
    data = db.Column(db.JSON, nullable=False, default=dict)


class PbsImport(db.Model):
    """One active PBS import per pilot — matches the old single global
    _pbs_store's behavior (a fresh import replaces the last one), just
    scoped per user instead of shared by every visitor."""
    __tablename__ = "pbs_imports"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    meta = db.Column(db.JSON)
    sequences = db.Column(db.JSON, default=list)
    updated_at = db.Column(db.DateTime(timezone=True), default=_now, onupdate=_now)


class SignatureLog(db.Model):
    """Audit trail of signing events — separate from Leg.data so it
    survives a leg being regenerated (re-signing overwrites the leg's own
    signature field but not this history)."""
    __tablename__ = "signature_log"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    leg_id = db.Column(db.Integer, nullable=False)
    flight_number = db.Column(db.String(16))
    dep_date = db.Column(db.String(16))
    signed_at = db.Column(db.String(64))
