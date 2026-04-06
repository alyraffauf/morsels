import json

# =============================================================================
# App setup
# =============================================================================
import os
import secrets
import sqlite3
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import regex
import requests
from atproto import Client, models
from atproto_client.exceptions import BadRequestError, NetworkError
from atproto_identity.resolver import IdResolver
from authlib.jose import JsonWebKey
from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, guess_lexer
from quart import (
    Quart,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException
from werkzeug.wrappers import Response as WerkzeugResponse

from atproto_oauth import (
    fetch_authserver_meta,
    initial_token_request,
    is_safe_url,
    pds_authed_req,
    refresh_token_request,
    resolve_pds_authserver,
    revoke_token_request,
    send_par_auth_request,
)
from config import load_config
from identity import (
    fetch_profile,
    fetch_recent_bites,
    fetch_replies,
    hydrate_replies,
    resolve_did,
    resolve_identity,
)

app = Quart(__name__)
DATA_DIR = os.environ.get("MORSEL_DATA_DIR", ".")
load_config(app, data_dir=DATA_DIR)

CLIENT_SECRET_JWK = JsonWebKey.import_key(json.loads(app.config["CLIENT_SECRET_JWK"]))
CLIENT_PUB_JWK = json.loads(CLIENT_SECRET_JWK.as_json(is_private=False))
assert "d" not in CLIENT_PUB_JWK

OAUTH_SCOPE = "atproto repo:blue.morsels.bite repo:blue.morsels.reply"
COLLECTION = "blue.morsels.bite"
SLINGSHOT_URL = "https://slingshot.microcosm.blue"

_avatar_cache: dict[str, tuple[bytes, str, float]] = {}
AVATAR_TTL = 3600

_bite_cache: dict[str, tuple[Any, float]] = {}
BITE_TTL = 3600

# =============================================================================
# Database
# =============================================================================


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = app.config.get("DATABASE_URL", os.path.join(DATA_DIR, "morsel.db"))
        g.db = sqlite3.connect(db_path)
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.row_factory = sqlite3.Row
    return g.db


def query_db(
    query: str, args: tuple | list = (), one: bool = False
) -> sqlite3.Row | list[sqlite3.Row] | None:
    db = get_db()
    cur = db.cursor()
    cur.execute(query, args)
    rv = cur.fetchall()
    db.commit()
    cur.close()
    return (rv[0] if rv else None) if one else rv


@app.before_serving
async def init_db() -> None:
    async with app.app_context():
        db = get_db()
        schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
        with open(schema_path, "r") as f:
            db.cursor().executescript(f.read())
        db.commit()


# =============================================================================
# Helpers
# =============================================================================


@app.route("/avatar/<path:did>")
async def avatar_proxy(did: str) -> WerkzeugResponse:
    now = time.time()

    if did in _avatar_cache:
        data, content_type, ts = _avatar_cache[did]
        if now - ts < AVATAR_TTL:
            return Response(data, mimetype=content_type)

    # Resolve PDS to find avatar
    handle, pds_url = await resolve_identity(did)
    if pds_url is None:
        return Response(status=404)

    profile = await fetch_profile(did, pds_url)
    cdn_url = profile.get("avatar_url")
    blob_url = profile.get("avatar_blob_url")
    if not cdn_url and not blob_url:
        return Response(status=404)

    resp = None
    async with httpx.AsyncClient() as http:
        for url in [cdn_url, blob_url]:
            if not url:
                continue
            try:
                resp = await http.get(url, timeout=5)
                if resp.status_code == 200:
                    break
            except (httpx.HTTPError, ValueError):
                continue

    if resp is None or resp.status_code != 200:
        return Response(status=502)

    content_type = resp.headers.get("Content-Type", "image/jpeg")
    _avatar_cache[did] = (resp.content, content_type, now)

    return Response(resp.content, mimetype=content_type)


def compute_client_id(url_root: str) -> tuple[str, str]:
    parsed = urlparse(url_root)
    if parsed.hostname in ["localhost", "127.0.0.1"]:
        redirect_uri = f"http://127.0.0.1:{parsed.port}/oauth/callback"
        client_id = "http://localhost?" + urlencode(
            {"redirect_uri": redirect_uri, "scope": OAUTH_SCOPE}
        )
    else:
        app_url = url_root.replace("http://", "https://")
        redirect_uri = f"{app_url}oauth/callback"
        client_id = f"{app_url}oauth-client-metadata.json"
    return client_id, redirect_uri


def highlight_code(content: str | None) -> str:
    lexer = guess_lexer(content) if content else TextLexer()
    formatter = HtmlFormatter(nowrap=True)
    return highlight(content or "", lexer, formatter).rstrip("\n")


async def require_identity(
    identifier: str, redirect_endpoint: str, **redirect_kwargs: Any
) -> tuple[str, str | None, str, dict] | WerkzeugResponse:
    """Resolve an identifier to a DID, redirecting handles to canonical DID URLs.

    Returns (did, handle, pds_url, profile) or redirects/aborts.
    Callers must check the return — if it's a Response (redirect), return it directly.
    """
    did = await resolve_did(identifier)
    if did is None:
        abort(404, "User not found")
    if did != identifier:
        return redirect(url_for(redirect_endpoint, identifier=did, **redirect_kwargs))

    handle, pds_url = await resolve_identity(did)
    if handle is None and pds_url is None:
        abort(404, "User not found.")
    if pds_url is None:
        abort(502, "Could not reach this user's server.")

    profile = await fetch_profile(did, pds_url)
    return did, handle, pds_url, profile


def fetch_bites(pds_url: str, did: str, limit: int = 100) -> list[dict[str, str]]:
    """Fetch bite records from a user's PDS. Returns a list of dicts."""
    try:
        client = Client(pds_url)
        response = client.com.atproto.repo.list_records(
            models.ComAtprotoRepoListRecords.Params(
                repo=did,
                collection=COLLECTION,
                limit=limit,
            )
        )
    except BadRequestError:
        abort(404, "User not found.")
    except NetworkError:
        abort(502, "Could not reach this user's server.")
    except Exception:
        abort(500, "Something went wrong loading bites.")

    bites = []
    for record in response.records:
        try:
            bites.append(
                {
                    "rkey": record.uri.split("/")[-1],
                    "title": record.value["title"],
                    "content": record.value["content"],
                    "created_at": record.value["createdAt"],
                }
            )
        except KeyError, TypeError:
            continue
    return bites


def authed_pds_request(
    method: str, path: str, body: dict | None = None
) -> WerkzeugResponse | requests.Response | None:
    """Make an authenticated request to the logged-in user's PDS, refreshing tokens if needed.

    Returns the response, or redirects to login on auth failure.
    """
    pds_url = g.user["pds_url"]
    did = g.user["did"]
    url = f"{pds_url}/xrpc/{path}"

    try:
        resp = pds_authed_req(method, url, user=g.user, db=get_db(), body=body)
    except Exception:
        flash("Request timed out. Try again.", "error")
        return redirect(request.referrer or url_for("index"))

    if resp.status_code == 401:  # type: ignore[union-attr]
        client_id, _ = compute_client_id(request.url_root)
        try:
            tokens, dpop_nonce = refresh_token_request(
                g.user,
                client_id,
                CLIENT_SECRET_JWK,
            )
            query_db(
                "UPDATE oauth_session SET access_token = ?, refresh_token = ?, dpop_authserver_nonce = ? WHERE did = ?;",
                [tokens["access_token"], tokens["refresh_token"], dpop_nonce, did],
            )
            g.user = query_db(
                "SELECT * FROM oauth_session WHERE did = ?",
                [did],
                one=True,
            )
            resp = pds_authed_req(method, url, user=g.user, db=get_db(), body=body)
        except Exception:
            flash("Session expired, please log in again", "error")
            return redirect(url_for("oauth_login"))

    return resp


def delete_record(
    collection: str, record_rkey: str
) -> WerkzeugResponse | requests.Response | None:
    """Delete a record from the logged-in user's repo."""
    body = {
        "repo": g.user["did"],
        "collection": collection,
        "rkey": record_rkey,
    }
    return authed_pds_request("POST", "com.atproto.repo.deleteRecord", body=body)


async def check_csrf() -> None:
    token = session.get("csrf_token")
    submitted = (await request.form).get("csrf_token")
    if not token or token != submitted:
        abort(400, "Invalid or missing security token. Please try again.")


# =============================================================================
# Hooks and filters
# =============================================================================


@app.teardown_appcontext
async def close_db(exception: BaseException | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.before_request
async def ensure_csrf_token() -> None:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)


@app.before_request
async def load_logged_in_user() -> None:
    user_did = session.get("user_did")
    if user_did is None:
        g.user = None
    else:
        g.user = query_db(
            "SELECT * FROM oauth_session WHERE did = ?", [user_did], one=True
        )


@app.template_filter("humandate")
def humandate_filter(value: str) -> str:
    """Return a <time> element that JS will convert to the viewer's local timezone."""
    try:
        dt = datetime.fromisoformat(value)
        iso = dt.isoformat()
        fallback = dt.strftime("%b %-d, %Y at %-I:%M %p").lower()
        return Markup(f'<time datetime="{iso}" class="js-localtime">{fallback}</time>')
    except Exception:
        return value


# =============================================================================
# Error handling
# =============================================================================


@app.errorhandler(403)
async def forbidden(e: HTTPException) -> tuple[str, int]:
    message = (
        e.description
        if e.description != "Forbidden"
        else "You don't have permission to do that."
    )
    return await render_template("error.html", code=403, message=message), 403


@app.errorhandler(400)
async def bad_request(e: HTTPException) -> tuple[str, int]:
    message = e.description if e.description != "Bad Request" else "Bad request."
    return await render_template("error.html", code=400, message=message), 400


@app.errorhandler(401)
async def unauthorized(e: HTTPException) -> tuple[str, int]:
    message = (
        e.description if e.description != "Unauthorized" else "You need to log in."
    )
    return await render_template("error.html", code=401, message=message), 401


@app.errorhandler(404)
async def not_found(e: HTTPException) -> tuple[str, int]:
    message = (
        e.description if e.description != "Not Found" else "That page doesn't exist."
    )
    return await render_template("error.html", code=404, message=message), 404


@app.errorhandler(500)
async def internal_error(e: HTTPException) -> tuple[str, int]:
    return await render_template(
        "error.html", code=500, message="Something went wrong on our end."
    ), 500


@app.errorhandler(502)
async def bad_gateway(e: HTTPException) -> tuple[str, int]:
    message = (
        e.description
        if e.description != "Bad Gateway"
        else "Couldn't reach the upstream server."
    )
    return await render_template("error.html", code=502, message=message), 502


# =============================================================================
# OAuth metadata
# =============================================================================


@app.route("/oauth-client-metadata.json")
async def oauth_client_metadata() -> WerkzeugResponse:
    app_url = request.url_root.replace("http://", "https://")
    client_id = f"{app_url}oauth-client-metadata.json"
    return jsonify(
        {
            "client_id": client_id,
            "dpop_bound_access_tokens": True,
            "application_type": "web",
            "redirect_uris": [f"{app_url}oauth/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": OAUTH_SCOPE,
            "token_endpoint_auth_method": "private_key_jwt",
            "token_endpoint_auth_signing_alg": "ES256",
            "jwks_uri": f"{app_url}oauth/jwks.json",
            "client_name": "Morsels",
            "client_uri": app_url,
        }
    )


@app.route("/oauth/jwks.json")
async def oauth_jwks() -> WerkzeugResponse:
    return jsonify({"keys": [CLIENT_PUB_JWK]})


@app.route("/pygments.css")
async def pygments_css() -> WerkzeugResponse:
    css = HtmlFormatter(style="default").get_style_defs()
    resp = Response(css, mimetype="text/css")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# =============================================================================
# OAuth flow
# =============================================================================


@app.route("/oauth/login", methods=("GET", "POST"))
async def oauth_login() -> WerkzeugResponse | str:
    if request.method != "POST":
        return redirect(url_for("index"))

    await check_csrf()
    username = (await request.form)["username"]
    username = regex.sub(r"[\p{C}]", "", username)
    if username.startswith("@"):
        username = username[1:]

    did = await resolve_did(username)
    if did is None:
        flash("Could not find that account. Check your handle and try again.", "error")
        return redirect(url_for("index"))

    handle, pds_url = await resolve_identity(did)
    if pds_url is None:
        flash("Could not reach your server. Try again later.", "error")
        return redirect(url_for("index"))

    try:
        authserver_url = resolve_pds_authserver(pds_url)
    except Exception:
        flash("Could not connect to your login provider. Try again later.", "error")
        return redirect(url_for("index"))

    try:
        authserver_meta = fetch_authserver_meta(authserver_url)
    except Exception:
        flash("Could not connect to your login provider. Try again later.", "error")
        return redirect(url_for("index"))

    dpop_private_jwk = JsonWebKey.generate_key("EC", "P-256", is_private=True)
    client_id, redirect_uri = compute_client_id(request.url_root)

    try:
        pkce_verifier, state, dpop_authserver_nonce, resp = send_par_auth_request(
            authserver_url,
            authserver_meta,
            username,
            client_id,
            redirect_uri,
            OAUTH_SCOPE,
            CLIENT_SECRET_JWK,
            dpop_private_jwk,
        )
    except Exception:
        flash("Login request timed out. Try again.", "error")
        return redirect(url_for("index"))

    if resp.status_code != 201:
        flash("Login request failed. Try again later.", "error")
        return redirect(url_for("index"))

    par_request_uri = resp.json()["request_uri"]

    query_db(
        "INSERT INTO oauth_auth_request (state, authserver_iss, did, handle, pds_url, pkce_verifier, scope, dpop_authserver_nonce, dpop_private_jwk) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?);",
        [
            state,
            authserver_meta["issuer"],
            did,
            handle,
            pds_url,
            pkce_verifier,
            OAUTH_SCOPE,
            dpop_authserver_nonce,
            dpop_private_jwk.as_json(is_private=True),
        ],
    )

    auth_url = authserver_meta["authorization_endpoint"]
    if not is_safe_url(auth_url):
        flash("Login failed due to a security issue. Try again later.", "error")
        return redirect(url_for("index"))
    qparam = urlencode({"client_id": client_id, "request_uri": par_request_uri})
    return redirect(f"{auth_url}?{qparam}")


@app.route("/oauth/callback")
async def oauth_callback() -> WerkzeugResponse:
    if request.args.get("error"):
        flash("Login was denied or failed. Please try again.", "error")
        return redirect(url_for("index"))

    state = request.args["state"]
    authserver_iss = request.args["iss"]
    code = request.args["code"]

    row = query_db(
        "SELECT * FROM oauth_auth_request WHERE state = ?;", [state], one=True
    )
    if row is None:
        flash("Login session expired. Please try again.", "error")
        return redirect(url_for("index"))

    query_db("DELETE FROM oauth_auth_request WHERE state = ?;", [state])

    if row["authserver_iss"] != authserver_iss:  # type: ignore[index]
        flash("Login failed due to a security issue. Please try again.", "error")
        return redirect(url_for("index"))

    client_id, redirect_uri = compute_client_id(request.url_root)
    try:
        tokens, dpop_authserver_nonce = initial_token_request(
            row,
            code,
            client_id,
            redirect_uri,
            CLIENT_SECRET_JWK,
        )
    except Exception:
        flash("Login failed — could not complete token exchange. Try again.", "error")
        return redirect(url_for("index"))

    if row["did"]:  # type: ignore[index]
        did, handle, pds_url = row["did"], row["handle"], row["pds_url"]  # type: ignore[index]
        if tokens["sub"] != did:
            flash("Login failed — identity mismatch. Please try again.", "error")
            return redirect(url_for("index"))
    else:
        did = tokens["sub"]
        resolver = IdResolver()
        did_doc = resolver.did.resolve(did)
        handle = None
        for aka in did_doc.also_known_as:  # type: ignore[union-attr]
            if aka.startswith("at://"):
                handle = aka[5:]
                break
        pds_url = None
        for svc in did_doc.service:  # type: ignore[union-attr]
            if svc.id == "#atproto_pds":
                pds_url = svc.service_endpoint
                break

    query_db(
        "INSERT OR REPLACE INTO oauth_session (did, handle, pds_url, authserver_iss, access_token, refresh_token, dpop_authserver_nonce, dpop_private_jwk) VALUES(?, ?, ?, ?, ?, ?, ?, ?);",
        [
            did,
            handle,
            pds_url,
            authserver_iss,
            tokens["access_token"],
            tokens["refresh_token"],
            dpop_authserver_nonce,
            row["dpop_private_jwk"],  # type: ignore[index]
        ],
    )

    session["user_did"] = did
    session["user_handle"] = handle
    return redirect(url_for("index"))


@app.route("/oauth/logout")
async def oauth_logout() -> WerkzeugResponse:
    if g.user:
        client_id, _ = compute_client_id(request.url_root)
        try:
            revoke_token_request(g.user, client_id, CLIENT_SECRET_JWK)
        except Exception:
            pass
        query_db("DELETE FROM oauth_session WHERE did = ?;", [g.user["did"]])

    session.clear()
    return redirect(url_for("index"))


# =============================================================================
# Bite routes
# =============================================================================


@app.route("/")
async def index() -> str:
    recent = await fetch_recent_bites(limit=5)

    # Pre-populate bite cache from feed data
    now = time.time()
    for bite in recent:
        key = f"{bite.get('did')}/{bite.get('rkey')}"
        if key not in _bite_cache:
            _bite_cache[key] = ({
                "title": bite.get("title", "Untitled"),
                "content": bite.get("content", ""),
                "created_at": bite.get("created_at", ""),
                "cid": "",
            }, now)

    if g.user:
        return await render_template(
            "create.html", recent=recent, did=g.user["did"], handle=g.user["handle"]
        )

    return await render_template("index.html", recent=recent)


@app.route("/b/new", methods=["POST"])
async def create_bite() -> WerkzeugResponse | str:
    if not g.user:
        return redirect(url_for("oauth_login"))
    await check_csrf()

    content: str = (await request.form)["content"]
    title: str = (await request.form).get("title", "").strip() or "Untitled"
    did: str = g.user["did"]

    body = {
        "repo": did,
        "collection": COLLECTION,
        "record": {
            "$type": COLLECTION,
            "title": title,
            "content": content,
            "createdAt": datetime.now().astimezone().isoformat(),
        },
    }

    resp = authed_pds_request("POST", "com.atproto.repo.createRecord", body=body)
    if isinstance(resp, WerkzeugResponse):
        return resp
    if resp is None or resp.status_code not in [200, 201]:
        abort(500, "Failed to create bite. Please try again.")

    rkey: str = resp.json()["uri"].split("/")[-1]
    return redirect(url_for("view_bite", identifier=did, rkey=rkey))


@app.route("/u/<identifier>")
async def list_bites(identifier: str) -> WerkzeugResponse | str:
    result = await require_identity(identifier, "list_bites")
    if not isinstance(result, tuple):
        return result
    did, handle, pds_url, profile = result

    pastes = fetch_bites(pds_url, did)

    return await render_template(
        "list.html",
        pastes=pastes,
        did=did,
        handle=handle,
        profile=profile,
    )


@app.route("/b/<path:identifier>/<rkey>")
async def view_bite(identifier: str, rkey: str) -> WerkzeugResponse | str:
    result = await require_identity(identifier, "view_bite", rkey=rkey)
    if not isinstance(result, tuple):
        return result
    did, handle, pds_url, profile = result

    cache_key = f"{did}/{rkey}"
    now = time.time()
    bite = None
    if cache_key in _bite_cache:
        cached, ts = _bite_cache[cache_key]
        if now - ts < BITE_TTL:
            bite = cached

    if bite is None:
        try:
            client = Client(SLINGSHOT_URL)
            response = client.com.atproto.repo.get_record(
                models.ComAtprotoRepoGetRecord.Params(
                    repo=did,
                    collection=COLLECTION,
                    rkey=rkey,
                )
            )
            bite = {
                "title": response.value["title"] or "Untitled",
                "content": response.value["content"] or "",
                "created_at": response.value["createdAt"] or "",
                "cid": response.cid,
            }
            _bite_cache[cache_key] = (bite, now)
        except BadRequestError:
            abort(404, "Bite not found.")
        except NetworkError:
            abort(502, "Could not reach this user's server.")
        except (KeyError, TypeError):
            abort(500, "This bite has missing or malformed data.")
        except Exception:
            abort(500, "Something went wrong loading this bite.")

    title = bite["title"]
    content = bite["content"]
    created_at = bite["created_at"]

    raw_replies = await fetch_replies(did, rkey)
    replies = await hydrate_replies(raw_replies)

    pending = session.pop("pending_reply", None)
    if pending:
        pending_did = pending.get("did")
        pending_text = pending.get("text")
        already_indexed = any(
            r["did"] == pending_did and r["text"] == pending_text for r in replies
        )
        if not already_indexed:
            replies.insert(0, pending)

    return await render_template(
        "view.html",
        title=title,
        paste_html=highlight_code(content),
        paste_raw=content,
        paste_id=f"{did}/{rkey}",
        did=did,
        handle=handle,
        profile=profile,
        created_at=created_at,
        at_uri=f"at://{did}/{COLLECTION}/{rkey}",
        cid=bite.get("cid", ""),
        replies=replies,
    )


@app.route("/b/<path:identifier>/<rkey>/reply", methods=["POST"])
async def create_reply(identifier: str, rkey: str) -> WerkzeugResponse:
    if not g.user:
        return redirect(url_for("oauth_login"))
    await check_csrf()

    text: str = (await request.form)["text"].strip()
    if not text:
        return redirect(url_for("view_bite", identifier=identifier, rkey=rkey))

    at_uri: str = (await request.form)["at_uri"]
    cid: str = (await request.form)["cid"]

    body = {
        "repo": g.user["did"],
        "collection": "blue.morsels.reply",
        "record": {
            "$type": "blue.morsels.reply",
            "text": text,
            "createdAt": datetime.now().astimezone().isoformat(),
            "subject": {
                "uri": at_uri,
                "cid": cid,
            },
        },
    }

    resp = authed_pds_request("POST", "com.atproto.repo.createRecord", body=body)
    if isinstance(resp, WerkzeugResponse):
        return resp

    reply_rkey: str | None = (
        resp.json().get("uri", "").split("/")[-1]
        if resp is not None and resp.status_code in [200, 201]
        else None
    )
    session["pending_reply"] = {
        "did": g.user["did"],
        "handle": g.user["handle"],
        "rkey": reply_rkey,
        "text": text,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    return redirect(url_for("view_bite", identifier=identifier, rkey=rkey))


@app.route("/b/<path:identifier>/<rkey>/delete", methods=["POST"])
async def delete_bite(identifier: str, rkey: str) -> WerkzeugResponse:
    if not g.user:
        return redirect(url_for("oauth_login"))
    await check_csrf()

    if await resolve_did(identifier) != g.user["did"]:
        abort(403, "You can only delete your own bites.")

    resp = delete_record(COLLECTION, rkey)
    if isinstance(resp, WerkzeugResponse):
        return resp

    _bite_cache.pop(f"{g.user['did']}/{rkey}", None)
    flash("Bite deleted.")
    return redirect(url_for("list_bites", identifier=g.user["did"]))


@app.route("/b/<path:identifier>/<rkey>/delete-reply", methods=["POST"])
async def delete_reply(identifier: str, rkey: str) -> WerkzeugResponse:
    if not g.user:
        return redirect(url_for("oauth_login"))
    await check_csrf()

    reply_rkey: str | None = (await request.form).get("reply_rkey")
    if not reply_rkey:
        abort(400)

    resp = delete_record("blue.morsels.reply", reply_rkey)
    if isinstance(resp, WerkzeugResponse):
        return resp

    flash("Reply deleted.")
    return redirect(url_for("view_bite", identifier=identifier, rkey=rkey))
