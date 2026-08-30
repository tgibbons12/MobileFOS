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
    # A paid FlightAware credential, persisted per pilot at their explicit
    # request (2026-08-19) — this reverses aeroapi.py's original "never
    # stored" design; that module's docstring has been updated to match.
    aeroapi_key = db.Column(db.String(255))
    # Replaces fos_last_leg — which leg this pilot was last on, so "Current
    # Flight" / "Request New Data" on Home can be computed server-side.
    current_leg_id = db.Column(db.Integer)
    # A saved default for the Pairing Library browser — {"opr","base",
    # "fleet","label"} — so it can open straight to "my bid" instead of
    # requiring opr->base->fleet to be picked every time.
    bid_shortcut = db.Column(db.JSON)
    # Individually starred pairings from the Library — [{"opr","base",
    # "fleet","seq"}, ...] — distinct from bid_shortcut (one default
    # opr/base/fleet) and from PbsImport.sequences (actually being flown);
    # this is just a personal shortlist for quick re-visits.
    saved_pairings = db.Column(db.JSON, default=list)
    # Saved filter criteria over one Pairing Library pack — "sort the
    # thousands of pairings" rather than browse them one by one. Each is
    # {"id","name","opr","base","fleet","properties":{...}} — see
    # _layer_matches() in server.py for what "properties" supports.
    bid_layers = db.Column(db.JSON, default=list)
    # IANA timezone name (e.g. "America/Phoenix") — controls what clock
    # face MOT is displayed in; blank means "show it as computed" (the
    # bid pack's own local time, no conversion).
    timezone = db.Column(db.String(64))
    # One promoted sequence a pilot has deliberately "picked up" as their
    # current trip (Schedule > My Trip's Pick Up button) — not leg-scoped
    # like current_leg_id, and only cleared by an explicit Close Trip.
    active_seq = db.Column(db.String(32))
    # Company-document publishing rights. Deliberately nullable with no DB
    # default so the migration is one portable ALTER on both SQLite and
    # Postgres (a BOOLEAN DEFAULT 0 is rejected by Postgres, DEFAULT FALSE
    # by older SQLite) — read it as bool(user.is_admin) everywhere, where
    # NULL means "not an admin". This is the app's first real role: unlike
    # pairing packs (uploaded on the honor system, user_id audit-only), a
    # published doc forces an acknowledgement on every other pilot, so who
    # can publish one is enforced rather than assumed.
    is_admin = db.Column(db.Boolean, default=False)
    # Last request this account made, for the admin roster. Written at most
    # once every few minutes rather than per request (see _touch_last_seen
    # in server.py) — an accurate-to-the-second value isn't worth a DB write
    # on every page load, and "last active" only needs to be roughly right.
    # Starts recording from deploy; there's no history to backfill.
    last_seen = db.Column(db.DateTime(timezone=True))
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
    # Staged leg-edit proposals awaiting confirm/reject, keyed by seq number —
    # {seq_number: {"sequence": <edited seq dict>, "violations": [...], "meta": {...}}}.
    # Same "stage it, let the user decide" shape as Leg.data's pending_date_slip.
    pending_edits = db.Column(db.JSON, default=dict)
    updated_at = db.Column(db.DateTime(timezone=True), default=_now, onupdate=_now)


class PairingPack(db.Model):
    """One bulk-imported PBS bid-pack file, tagged by operator/base/fleet —
    the browsable "master library" distinct from PbsImport.sequences (the
    pilot's own currently active/flown pairings). Browsing is
    opr -> base -> fleet -> sequence; picking one sequence promotes a copy
    into PbsImport.sequences (see /pbs/packs/.../promote in server.py),
    reusing the exact same downstream leg-generation machinery as any other
    imported sequence.

    A dedicated table rather than a JSON blob on PbsImport deliberately —
    at bulk-library scale (thousands of sequences per pack, dozens of
    packs) a "list all packs with counts" query needs to stay cheap without
    ever deserializing every pack's own (large) `sequences` column; storing
    `seq_count` as its own column and querying it column-limited
    (db.session.query(PairingPack.opr, ...)) keeps that listing fast
    regardless of how big any individual pack's sequences list gets."""
    __tablename__ = "pairing_packs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    opr = db.Column(db.String(8), nullable=False, index=True)
    base = db.Column(db.String(8), nullable=False, index=True)
    fleet = db.Column(db.String(8), nullable=False, index=True)
    seq_count = db.Column(db.Integer, nullable=False, default=0)
    meta = db.Column(db.JSON)
    sequences = db.Column(db.JSON, default=list)
    updated_at = db.Column(db.DateTime(timezone=True), default=_now, onupdate=_now)
    __table_args__ = (
        db.UniqueConstraint("user_id", "opr", "base", "fleet", name="uq_pack_user_opr_base_fleet"),
    )


class ReleaseCache(db.Model):
    """The last-generated release PDFs for a leg, keyed 1:1 on the leg —
    generate_release_pdfs() takes up to a minute (it's a real SimBrief OFP
    fetch + PDF render), so both the Confirm view's "Generate Release"
    button and the Documents PDF viewer hit this cache instead of each
    independently re-running it on every click/page load. payload holds
    the same {rls_pdf_b64, wb_pdf_b64, fi_pdf_b64, ...} dict the /release
    route already returned before caching existed."""
    __tablename__ = "release_cache"
    id = db.Column(db.Integer, primary_key=True)
    leg_id = db.Column(db.Integer, db.ForeignKey("legs.id"), unique=True, nullable=False)
    filename = db.Column(db.String(255))
    payload = db.Column(db.JSON, nullable=False, default=dict)
    generated_at = db.Column(db.DateTime(timezone=True), default=_now)


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


class TripCheckIn(db.Model):
    """A separate signature audit trail for picking up a trip (Schedule >
    My Trip's Pick Up button) — distinct from SignatureLog's per-LEG FFD/
    eFlight-Plan signatures, since a trip check-in is scoped to a sequence
    number, not any one leg. A brand-new table rather than loosening
    SignatureLog.leg_id's NOT NULL constraint, which would need a
    Postgres-vs-SQLite-aware ALTER COLUMN migration for no real benefit."""
    __tablename__ = "trip_checkins"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    seq = db.Column(db.String(32), nullable=False, index=True)
    signature = db.Column(db.Text)
    signed_at = db.Column(db.String(64))


class Document(db.Model):
    """One company document published to every pilot on this instance —
    same instance-wide sharing model as PairingPack (upload once, everyone
    sees it), but publishing is admin-gated because every pilot has to
    acknowledge it.

    `slug` is the stable identity across revisions (derived from the
    filename, the same way a pack is keyed by opr/base/fleet rather than by
    row id): re-uploading the same slug bumps `revision` and clears that
    document's acknowledgements, so a revised manual has to be
    re-acknowledged rather than silently inheriting the old sign-offs.

    The PDF rides as base64 in a Text column, matching ReleaseCache's
    already-proven approach for storing generated PDFs — keeps this to one
    table with no filesystem/object-store dependency, which matters on
    Railway where the app's own disk is ephemeral."""
    __tablename__ = "documents"
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(160), unique=True, nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    filename = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(64))
    pdf_b64 = db.Column(db.Text, nullable=False)
    size_bytes = db.Column(db.Integer, default=0)
    revision = db.Column(db.Integer, nullable=False, default=1)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    uploaded_at = db.Column(db.DateTime(timezone=True), default=_now)


class DocumentAck(db.Model):
    """One pilot's acknowledgement of one document revision. `revision` is
    stored on the ack (not just inferred) so the audit trail still says
    which version was actually acknowledged after the doc is revised —
    a re-upload deletes the outstanding acks, but this row's revision
    records what the pilot saw when they signed off."""
    __tablename__ = "document_acks"
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    revision = db.Column(db.Integer, nullable=False, default=1)
    acknowledged_at = db.Column(db.String(64))
    __table_args__ = (
        db.UniqueConstraint("document_id", "user_id", name="uq_ack_doc_user"),
    )
