"""
Demand Forecasting Chains API
"""

import os
import sqlite3
from collections import defaultdict

from flask import Flask, jsonify, request

app = Flask(__name__)

# ── Configuration ─────────────────────────────────────────────────────

DB_PATH = os.path.join(os.path.dirname(__file__), "chains.db") # SQLite file path

COUNTRIES = [
    ("PL", "Poland"),
    ("DE", "Germany"),
    ("US", "United States"),
    ("JP", "Japan"),
    ("CN", "China"),
]

CODE_TYPES = [
    ("IPC", "Internal Product Code"),
    ("GTIN", "Global Trade Item Number"),
]


# ── Database ──────────────────────────────────────────────────────────

# Database helper functions
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create all tables on first run (not if they already exist)"""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS countries (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS code_types (
                id   TEXT PRIMARY KEY,
                type TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                iso_country_code TEXT NOT NULL,
                comment          TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS code_transitions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id     INTEGER NOT NULL,
                code_type_id TEXT NOT NULL,
                type         TEXT NOT NULL,
                date         TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS introductions (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                code_transition_id INTEGER NOT NULL,
                introduction_code  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discontinuations (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                code_transition_id   INTEGER NOT NULL,
                discontinuation_code INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chains (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                code_transition_id   INTEGER NOT NULL,
                introduction_code    INTEGER NOT NULL,
                discontinuation_code INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS product_families (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                code_type_id     TEXT NOT NULL,
                identifier       TEXT NOT NULL,
                iso_country_code TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generations (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                product_family_id  INTEGER NOT NULL,
                introduction_id    INTEGER NOT NULL,
                discontinuation_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS generation_links (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                predecessor_id       INTEGER NOT NULL,
                successor_id         INTEGER NOT NULL,
                source_transition_id INTEGER NOT NULL
            );
        """)


init_db()


# ── Helpers ───────────────────────────────────────────────────────────
# Low-level DB helpers used by the validation and recompute logic
# Each helper takes an open connection plus the context it needs (country, code_type, code) and returns the relevant information

def _country_exists(conn, code):
    return conn.execute(
        "SELECT 1 FROM countries WHERE code = ?", (code,)
    ).fetchone() is not None


def _code_type_exists(conn, id_):
    return conn.execute(
        "SELECT 1 FROM code_types WHERE id = ?", (id_,)
    ).fetchone() is not None


def _intro_count(conn, country, code_type, code):
    """Number of times code has been introduced in this (country, code_type) context."""
    row = conn.execute("""
        SELECT COUNT(*) AS cnt
        FROM introductions i
        JOIN code_transitions ct ON i.code_transition_id = ct.id
        JOIN events e             ON ct.event_id          = e.id
        WHERE i.introduction_code = ?
          AND ct.code_type_id     = ?
          AND e.iso_country_code  = ?
    """, (code, code_type, country)).fetchone()
    return row["cnt"]


def _discont_count(conn, country, code_type, code):
    """Number of times code has been discontinued in this (country, code_type) context."""
    row = conn.execute("""
        SELECT COUNT(*) AS cnt
        FROM discontinuations d
        JOIN code_transitions ct ON d.code_transition_id = ct.id
        JOIN events e             ON ct.event_id          = e.id
        WHERE d.discontinuation_code = ?
          AND ct.code_type_id        = ?
          AND e.iso_country_code     = ?
    """, (code, code_type, country)).fetchone()
    return row["cnt"]


def _code_exists(conn, country, code_type, code):
    """True if code has ever been introduced in this context."""
    return _intro_count(conn, country, code_type, code) > 0


def _code_is_active(conn, country, code_type, code):
    """True if code has more introductions than discontinuations (net active)."""
    return _intro_count(conn, country, code_type, code) > \
           _discont_count(conn, country, code_type, code)


def _latest_discont_date(conn, country, code_type, code):
    """Most recent discontinuation date for this code, or None if never discontinued."""
    row = conn.execute("""
        SELECT MAX(ct.date) AS d
        FROM discontinuations d
        JOIN code_transitions ct ON d.code_transition_id = ct.id
        JOIN events e             ON ct.event_id          = e.id
        WHERE d.discontinuation_code = ?
          AND ct.code_type_id        = ?
          AND e.iso_country_code     = ?
    """, (code, code_type, country)).fetchone()
    return row["d"]


def _validate_event(conn, country, transitions):
    """
    Validate all transitions for a new event against the 13 business rules.

    Returns (True, None) when the event is valid.
    Returns (False, error_message) on the first rule violation found.

    Validation is done in two passes:
      Pass 1 — structural checks (required fields, valid references).
               Also collects codes introduced within this same event so
               that chain validation can reference them (same-event look-ahead).
      Pass 2 — business-rule checks (active state, overlaps, existence).
    """
    # Rule 11 — invalid country code
    if not _country_exists(conn, country):
        return False, f"Invalid country code: {country!r}"

    # Pass 1
    same_event_intros = set()   # (code_type, code) pairs introduced in this event

    for t in transitions:
        code_type = t.get("code_type_id")
        # Rule 12 — invalid code type
        if not code_type or not _code_type_exists(conn, code_type):
            return False, f"Invalid code_type_id: {code_type!r}"
        # Rule 13 — missing date
        if not t.get("date"):
            return False, "Missing date"

        t_type = t.get("type")
        if t_type == "INTRO":
            # Rule 8 — introduction missing introduction_code
            if t.get("introduction_code") is None:
                return False, "INTRO missing introduction_code"
            same_event_intros.add((code_type, t["introduction_code"]))
        elif t_type == "DISCONT":
            # Rule 9 — discontinuation missing discontinuation_code
            if t.get("discontinuation_code") is None:
                return False, "DISCONT missing discontinuation_code"
        elif t_type == "chain":
            # Rule 7 — chain missing discontinuation_code
            if t.get("discontinuation_code") is None:
                return False, "chain missing discontinuation_code"
            if t.get("introduction_code") is None:
                return False, "chain missing introduction_code"
        else:
            return False, f"Unknown transition type: {t_type!r}"

    # Pass 2
    for t in transitions:
        code_type = t["code_type_id"]
        date      = t["date"]
        t_type    = t["type"]

        if t_type == "INTRO":
            code = t["introduction_code"]
            # Rule 1 — double introduction of an active code
            if _code_is_active(conn, country, code_type, code):
                return False, f"Double introduction: code {code} is already active"
            # Rule 2 — overlapping generation (intro at earlier date)
            if _code_exists(conn, country, code_type, code):
                last_discont = _latest_discont_date(conn, country, code_type, code)
                if last_discont is not None and last_discont > date:
                    return False, (
                        f"Overlapping generation: code {code} was discontinued at "
                        f"{last_discont}, cannot reintroduce at {date}"
                    )

        elif t_type == "DISCONT":
            code     = t["discontinuation_code"]
            in_event = (code_type, code) in same_event_intros
            # Rule 6 - discontinuation of a never-introduced code
            if not _code_exists(conn, country, code_type, code) and not in_event:
                return False, f"Discontinuation of never-introduced code: {code}"
            # Rule 3 - double discontinuation (code already discontinued)
            if not _code_is_active(conn, country, code_type, code) and not in_event:
                return False, f"Double discontinuation: code {code} is not active"

        elif t_type == "chain":
            ic = t["introduction_code"]
            dc = t["discontinuation_code"]
            # Rule 10 — chain where intro == discont code (cannot self-replace)
            if ic == dc:
                return False, "Chain: introduction_code and discontinuation_code must differ"
            # Rule 5 — introduction_code must exist (in DB or same event)
            if not _code_exists(conn, country, code_type, ic) \
               and (code_type, ic) not in same_event_intros:
                return False, f"Chain: introduction_code {ic} does not exist"
            # Rule 4 — chain with non-existing discontinuation code (predecessor must exist)
            if not _code_exists(conn, country, code_type, dc) \
               and (code_type, dc) not in same_event_intros:
                return False, f"Chain: discontinuation_code {dc} does not exist"

    return True, None


def _make_union_find():
    """
    Return a (find, union) pair backed by a shared parent dict.

    find(x) — returns the root representative of x's component. Uses iterative path compression.
    union(a, b) — merges the components containing a and b.
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        # path compression
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    return find, union


def _recompute_families():
    """
    Rebuild product families using Union-Find over chain edges.

    Steps:
      1. Clear existing product_families, generations, generation_links.
      2. For each (country, code_type) context:
         a. Initialise one Union-Find node per introduction code.
         b. Union(predecessor, successor) for every chain.
         c. Each weakly-connected component becomes one ProductFamily.
         d. Each introduction becomes one Generation (linked to its discontinuation if one exists).
         e. Each chain becomes one GenerationLink between two generations.
    """
    with get_db() as conn:

        conn.executescript("""
            DELETE FROM generation_links;
            DELETE FROM generations;
            DELETE FROM product_families;
        """)

        intros = conn.execute("""
            SELECT i.id AS intro_id,
                   i.introduction_code,
                   ct.code_type_id,
                   e.iso_country_code,
                   ct.id   AS ct_id,
                   ct.date
            FROM introductions i
            JOIN code_transitions ct ON i.code_transition_id = ct.id
            JOIN events e ON ct.event_id = e.id
        """).fetchall()

        disconts = conn.execute("""
            SELECT d.id AS discont_id,
                   d.discontinuation_code,
                   ct.code_type_id,
                   e.iso_country_code,
                   ct.date
            FROM discontinuations d
            JOIN code_transitions ct ON d.code_transition_id = ct.id
            JOIN events e ON ct.event_id = e.id
        """).fetchall()

        chains = conn.execute("""
            SELECT c.introduction_code,
                   c.discontinuation_code,
                   ct.code_type_id,
                   e.iso_country_code,
                   ct.id AS ct_id
            FROM chains c
            JOIN code_transitions ct ON c.code_transition_id = ct.id
            JOIN events e ON ct.event_id = e.id
        """).fetchall()

        # Build discont lookup: (country, code_type, code) - discont_id
        # For codes with multiple generations keep the most recent discontinuation
        discont_map = {}
        for d in disconts:
            key      = (d["iso_country_code"], d["code_type_id"], d["discontinuation_code"])
            existing = discont_map.get(key)
            if existing is None or d["date"] > existing[1]:
                discont_map[key] = (d["discont_id"], d["date"])
        discont_id_map = {k: v[0] for k, v in discont_map.items()}

        # Group introductions and chains by (country, code_type) context
        contexts = defaultdict(lambda: {"intros": [], "chains": []})
        for intro in intros:
            ctx = (intro["iso_country_code"], intro["code_type_id"])
            contexts[ctx]["intros"].append(dict(intro))
        for chain in chains:
            ctx = (chain["iso_country_code"], chain["code_type_id"])
            contexts[ctx]["chains"].append(dict(chain))

        family_counter = defaultdict(int)

        for (country, code_type), data in contexts.items():
            intro_list = data["intros"]
            chain_list = data["chains"]

            find, union = _make_union_find()

            # Initialise every introduction code as its own component
            for intro in intro_list:
                find(intro["introduction_code"])

            # Merge components connected by chains (predecessor - successor)
            for chain in chain_list:
                union(chain["discontinuation_code"], chain["introduction_code"])

            # Group intro records by component root
            component_intros = defaultdict(list)
            for intro in intro_list:
                component_intros[find(intro["introduction_code"])].append(intro)

            # Group chain records by component root
            component_chains = defaultdict(list)
            for chain in chain_list:
                component_chains[find(chain["introduction_code"])].append(chain)

            for root, comp_intros in component_intros.items():
                # Create one ProductFamily per component
                family_counter[(country, code_type)] += 1
                identifier = f"{country}-{code_type}-{family_counter[(country, code_type)]:04d}"

                cur = conn.execute(
                    "INSERT INTO product_families (code_type_id, identifier, iso_country_code) VALUES (?, ?, ?)",
                    (code_type, identifier, country),
                )
                family_id = cur.lastrowid

                # Create one Generation per introduction in this component
                gen_map = {}   # introduction_code - generation_id
                for intro in comp_intros:
                    code       = intro["introduction_code"]
                    discont_id = discont_id_map.get((country, code_type, code))
                    cur = conn.execute(
                        "INSERT INTO generations (product_family_id, introduction_id, discontinuation_id) VALUES (?, ?, ?)",
                        (family_id, intro["intro_id"], discont_id),
                    )
                    gen_map[code] = cur.lastrowid

                # Create one GenerationLink per chain in this component
                for chain in component_chains[root]:
                    pred = gen_map.get(chain["discontinuation_code"])
                    succ = gen_map.get(chain["introduction_code"])
                    if pred and succ:
                        conn.execute(
                            "INSERT INTO generation_links (predecessor_id, successor_id, source_transition_id) VALUES (?, ?, ?)",
                            (pred, succ, chain["ct_id"]),
                        )


# ── Setup ─────────────────────────────────────────────────────────────

@app.route("/api/setup/", methods=["POST"])
def setup():
    """
    Reset state and seed reference data in the local database.

    This endpoint:
    1. Deletes ALL existing data (events cascade to all transitions)
    2. Ensures all COUNTRIES exist
    3. Ensures all CODE_TYPES exist

    Returns HTTP 200 when ready.
    """
    with get_db() as conn:
        conn.executescript("""
            DELETE FROM generation_links;
            DELETE FROM generations;
            DELETE FROM product_families;
            DELETE FROM chains;
            DELETE FROM discontinuations;
            DELETE FROM introductions;
            DELETE FROM code_transitions;
            DELETE FROM events;
        """)
        for code, name in COUNTRIES:
            conn.execute(
                "INSERT OR IGNORE INTO countries (code, name) VALUES (?, ?)", (code, name)
            )
        for ct_id, ct_type in CODE_TYPES:
            conn.execute(
                "INSERT OR IGNORE INTO code_types (id, type) VALUES (?, ?)", (ct_id, ct_type)
            )
    return jsonify({"status": "ok"}), 200


# ── Events ────────────────────────────────────────────────────────────

@app.route("/api/events/", methods=["GET"])
def list_events():
    """List all events"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, iso_country_code, comment FROM events"
        ).fetchall()
    return jsonify({"results": [dict(r) for r in rows]}), 200


@app.route("/api/events/", methods=["POST"])
def create_event():
    """Create a new event with transitions. 
    Return 201 on success, 400 if any rule is violated."""
    data        = request.get_json() or {}
    country     = data.get("iso_country_code", "")
    transitions = data.get("transitions_write", [])
    comment     = data.get("comment", "")

    with get_db() as conn:
        ok, err = _validate_event(conn, country, transitions)
        if not ok:
            return jsonify({"error": err}), 400

        cur = conn.execute(
            "INSERT INTO events (iso_country_code, comment) VALUES (?, ?)",
            (country, comment),
        )
        event_id = cur.lastrowid

        for t in transitions:
            cur = conn.execute(
                "INSERT INTO code_transitions (event_id, code_type_id, type, date) VALUES (?, ?, ?, ?)",
                (event_id, t["code_type_id"], t["type"], t["date"]),
            )
            ct_id = cur.lastrowid

            if t["type"] == "INTRO":
                conn.execute(
                    "INSERT INTO introductions (code_transition_id, introduction_code) VALUES (?, ?)",
                    (ct_id, t["introduction_code"]),
                )
            elif t["type"] == "DISCONT":
                conn.execute(
                    "INSERT INTO discontinuations (code_transition_id, discontinuation_code) VALUES (?, ?)",
                    (ct_id, t["discontinuation_code"]),
                )
            elif t["type"] == "chain":
                conn.execute(
                    "INSERT INTO chains (code_transition_id, introduction_code, discontinuation_code) VALUES (?, ?, ?)",
                    (ct_id, t["introduction_code"], t["discontinuation_code"]),
                )

    return jsonify({"id": event_id}), 201


@app.route("/api/events/<int:event_id>/", methods=["GET"])
def get_event(event_id):
    """Get a single event by ID"""
    with get_db() as conn:
        event = conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
    if not event:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(event)), 200


@app.route("/api/events/<int:event_id>/", methods=["DELETE"])
def delete_event(event_id):
    """Delete a single event and all its transitions"""
    with get_db() as conn:
        ct_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM code_transitions WHERE event_id = ?", (event_id,)
        ).fetchall()]
        for ct_id in ct_ids:
            conn.execute("DELETE FROM introductions    WHERE code_transition_id = ?", (ct_id,))
            conn.execute("DELETE FROM discontinuations WHERE code_transition_id = ?", (ct_id,))
            conn.execute("DELETE FROM chains           WHERE code_transition_id = ?", (ct_id,))
        conn.execute("DELETE FROM code_transitions WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM events           WHERE id = ?",       (event_id,))
    return "", 204


# ── Product Families ─────────────────────────────────────────────────

@app.route("/api/product-families/", methods=["GET"])
def list_families():
    """List all product families"""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM product_families").fetchall()
    return jsonify({"results": [dict(r) for r in rows]}), 200


@app.route("/api/product-families/<int:family_id>/", methods=["GET"])
def get_family(family_id):
    """Get a single product family with its generations and links"""
    with get_db() as conn:
        family = conn.execute(
            "SELECT * FROM product_families WHERE id = ?", (family_id,)
        ).fetchone()
        if not family:
            return jsonify({"error": "Not found"}), 404
        gens = conn.execute(
            "SELECT * FROM generations WHERE product_family_id = ?", (family_id,)
        ).fetchall()
        links = conn.execute("""
            SELECT gl.* FROM generation_links gl
            JOIN generations g ON gl.predecessor_id = g.id
            WHERE g.product_family_id = ?
        """, (family_id,)).fetchall()
    return jsonify({
        "family":      dict(family),
        "generations": [dict(g) for g in gens],
        "links":       [dict(l) for l in links],
    }), 200


@app.route("/api/product-families/recompute/", methods=["POST"])
def recompute():
    """Trigger family recomputation"""
    _recompute_families()
    return jsonify({"status": "ok"}), 200


# ── Resolution ───────────────────────────────────────────────────────

@app.route("/api/resolve/", methods=["GET"])
def resolve():
    """Resolve a code + date + country to its product family identifier"""

    code      = request.args.get("code")
    code_type = request.args.get("code_type")
    country   = request.args.get("country")
    date      = request.args.get("date")

    if not all([code, code_type, country, date]):
        return jsonify({"error": "Missing query parameters"}), 400

    with get_db() as conn:
        row = conn.execute("""
            SELECT pf.identifier, pf.id AS family_id
            FROM generations g
            JOIN introductions    i   ON g.introduction_id    = i.id
            JOIN code_transitions ict ON i.code_transition_id = ict.id
            JOIN events           ie  ON ict.event_id         = ie.id
            JOIN product_families pf  ON g.product_family_id  = pf.id
            LEFT JOIN discontinuations  d   ON g.discontinuation_id  = d.id
            LEFT JOIN code_transitions  dct ON d.code_transition_id  = dct.id
            WHERE i.introduction_code  = ?
              AND ict.code_type_id     = ?
              AND ie.iso_country_code  = ?
              AND ict.date            <= ?
              AND (g.discontinuation_id IS NULL OR dct.date > ?)
            LIMIT 1
        """, (code, code_type, country, date, date)).fetchone()

    if not row:
        return jsonify({"error": "Not found"}), 404

    return jsonify({
        "product_family_identifier": row["identifier"],
        "identifier":                row["identifier"],
        "product_family_id":         row["family_id"],
    }), 200


@app.route("/api/resolve/reverse/", methods=["GET"])
def resolve_reverse():
    """Reverse-resolve a family identifier + date to its active codes"""

    identifier = request.args.get("identifier")
    date       = request.args.get("date")

    if not identifier or not date:
        return jsonify({"error": "Missing query parameters"}), 400

    with get_db() as conn:
        rows = conn.execute("""
            SELECT i.introduction_code
            FROM generations g
            JOIN product_families pf  ON g.product_family_id  = pf.id
            JOIN introductions    i   ON g.introduction_id    = i.id
            JOIN code_transitions ict ON i.code_transition_id = ict.id
            LEFT JOIN discontinuations  d   ON g.discontinuation_id  = d.id
            LEFT JOIN code_transitions  dct ON d.code_transition_id  = dct.id
            WHERE pf.identifier  = ?
              AND ict.date       <= ?
              AND (g.discontinuation_id IS NULL OR dct.date > ?)
        """, (identifier, date, date)).fetchall()

    return jsonify({"codes": [r["introduction_code"] for r in rows]}), 200


@app.route("/api/resolve/bulk/", methods=["POST"])
def resolve_bulk():
    """Bulk version of resolve. Return a map of code to family identifier"""

    data      = request.get_json() or {}
    codes     = data.get("codes", [])
    date      = data.get("date")
    code_type = data.get("code_type")
    country   = data.get("country")

    results = {}
    with get_db() as conn:
        for code in codes:
            row = conn.execute("""
                SELECT pf.identifier
                FROM generations g
                JOIN introductions    i   ON g.introduction_id    = i.id
                JOIN code_transitions ict ON i.code_transition_id = ict.id
                JOIN events           ie  ON ict.event_id         = ie.id
                JOIN product_families pf  ON g.product_family_id  = pf.id
                LEFT JOIN discontinuations  d   ON g.discontinuation_id  = d.id
                LEFT JOIN code_transitions  dct ON d.code_transition_id  = dct.id
                WHERE i.introduction_code  = ?
                  AND ict.code_type_id     = ?
                  AND ie.iso_country_code  = ?
                  AND ict.date            <= ?
                  AND (g.discontinuation_id IS NULL OR dct.date > ?)
                LIMIT 1
            """, (code, code_type, country, date, date)).fetchone()
            results[str(code)] = row["identifier"] if row else None

    return jsonify(results), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)