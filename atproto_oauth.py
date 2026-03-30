"""ATProto OAuth helpers.

Handles DPoP proof generation, PAR requests, token exchange, and
authenticated PDS requests. Adapted from the Bluesky cookbook example.
"""

import json
import time
import urllib.request
from typing import Any, Tuple

import requests_hardened
from authlib.common.security import generate_token
from authlib.jose import JsonWebKey, jwt
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from requests import Response

# SSRF-safe HTTP client
hardened_http = requests_hardened.Manager(
    requests_hardened.Config(
        default_timeout=(2, 10),
        never_redirect=True,
        ip_filter_enable=True,
        ip_filter_allow_loopback_ips=False,
        user_agent_override="Morsels",
    )
)


def is_safe_url(url):
    """Crude SSRF check — only allows HTTPS URLs with public hostnames."""
    from urllib.parse import urlparse

    parts = urlparse(url)
    if not (
        parts.scheme == "https"
        and parts.hostname is not None
        and parts.hostname == parts.netloc
        and parts.username is None
        and parts.password is None
        and parts.port is None
    ):
        return False
    segments = parts.hostname.split(".")
    if not (
        len(segments) >= 2
        and segments[-1] not in ["local", "arpa", "internal", "localhost"]
    ):
        return False
    if segments[-1].isdigit():
        return False
    return True


def is_valid_authserver_meta(obj, url):
    """Validate authorization server metadata against atproto requirements."""
    from urllib.parse import urlparse

    fetch_url = urlparse(url)
    issuer_url = urlparse(obj["issuer"])
    assert issuer_url.hostname == fetch_url.hostname
    assert issuer_url.scheme == "https"
    assert "code" in obj["response_types_supported"]
    assert "authorization_code" in obj["grant_types_supported"]
    assert "refresh_token" in obj["grant_types_supported"]
    assert "S256" in obj["code_challenge_methods_supported"]
    assert "private_key_jwt" in obj["token_endpoint_auth_methods_supported"]
    assert "ES256" in obj["token_endpoint_auth_signing_alg_values_supported"]
    assert "atproto" in obj["scopes_supported"]
    assert obj["authorization_response_iss_parameter_supported"] is True
    assert obj["pushed_authorization_request_endpoint"] is not None
    assert obj["require_pushed_authorization_requests"] is True
    assert "ES256" in obj["dpop_signing_alg_values_supported"]
    assert obj["client_id_metadata_document_supported"] is True
    return True


def resolve_pds_authserver(url):
    """Given a PDS URL, find its authorization server."""
    assert is_safe_url(url)
    with hardened_http.get_session() as sess:
        resp = sess.get(f"{url}/.well-known/oauth-protected-resource")
    resp.raise_for_status()
    assert resp.status_code == 200
    return resp.json()["authorization_servers"][0]


def fetch_authserver_meta(url):
    """Fetch and validate authorization server metadata."""
    assert is_safe_url(url)
    with hardened_http.get_session() as sess:
        resp = sess.get(f"{url}/.well-known/oauth-authorization-server")
    resp.raise_for_status()
    meta = resp.json()
    assert is_valid_authserver_meta(meta, url)
    return meta


def client_assertion_jwt(client_id, authserver_url, client_secret_jwk):
    """Create a signed JWT asserting our client identity."""
    return jwt.encode(
        {"alg": "ES256", "kid": client_secret_jwk["kid"]},
        {
            "iss": client_id,
            "sub": client_id,
            "aud": authserver_url,
            "jti": generate_token(),
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        client_secret_jwk,
    ).decode("utf-8")


def authserver_dpop_jwt(method, url, nonce, dpop_private_jwk):
    """Create a DPoP proof JWT for auth server requests."""
    dpop_pub_jwk = json.loads(dpop_private_jwk.as_json(is_private=False))
    body = {
        "jti": generate_token(),
        "htm": method,
        "htu": url,
        "iat": int(time.time()),
        "exp": int(time.time()) + 30,
    }
    if nonce:
        body["nonce"] = nonce
    return jwt.encode(
        {"typ": "dpop+jwt", "alg": "ES256", "jwk": dpop_pub_jwk},
        body,
        dpop_private_jwk,
    ).decode("utf-8")


def _parse_www_authenticate(data):
    scheme, _, params = data.partition(" ")
    items = urllib.request.parse_http_list(params)
    opts = urllib.request.parse_keqv_list(items)
    return scheme, opts


def is_use_dpop_nonce_error_response(resp):
    """Check if a response is asking us to retry with a new DPoP nonce."""
    if resp.status_code not in [400, 401]:
        return False
    www_authenticate = resp.headers.get("WWW-Authenticate")
    if www_authenticate:
        try:
            scheme, params = _parse_www_authenticate(www_authenticate)
            if scheme.lower() == "dpop" and params.get("error") == "use_dpop_nonce":
                return True
        except Exception:
            pass
    try:
        json_body = resp.json()
        if isinstance(json_body, dict) and json_body.get("error") == "use_dpop_nonce":
            return True
    except Exception:
        pass
    return False


def auth_server_post(
    authserver_url,
    client_id,
    client_secret_jwk,
    dpop_private_jwk,
    dpop_authserver_nonce,
    post_url,
    post_data,
) -> Tuple[str, Response]:
    """POST to auth server with client assertion and DPoP, handling nonce rotation."""
    client_assertion = client_assertion_jwt(
        client_id, authserver_url, client_secret_jwk
    )
    post_data |= {
        "client_id": client_id,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": client_assertion,
    }
    dpop_proof = authserver_dpop_jwt(
        "POST", post_url, dpop_authserver_nonce, dpop_private_jwk
    )

    assert is_safe_url(post_url)
    with hardened_http.get_session() as sess:
        resp = sess.post(post_url, data=post_data, headers={"DPoP": dpop_proof})

    if is_use_dpop_nonce_error_response(resp):
        dpop_authserver_nonce = resp.headers["DPoP-Nonce"]
        dpop_proof = authserver_dpop_jwt(
            "POST", post_url, dpop_authserver_nonce, dpop_private_jwk
        )
        with hardened_http.get_session() as sess:
            resp = sess.post(post_url, data=post_data, headers={"DPoP": dpop_proof})

    return dpop_authserver_nonce, resp


def send_par_auth_request(
    authserver_url,
    authserver_meta,
    login_hint,
    client_id,
    redirect_uri,
    scope,
    client_secret_jwk,
    dpop_private_jwk,
) -> Tuple[str, str, str, Any]:
    """Send a Pushed Authorization Request. Returns (pkce_verifier, state, dpop_nonce, response)."""
    par_url = authserver_meta["pushed_authorization_request_endpoint"]
    state = generate_token()
    pkce_verifier = generate_token(48)
    code_challenge = create_s256_code_challenge(pkce_verifier)

    par_body = {
        "response_type": "code",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "redirect_uri": redirect_uri,
        "scope": scope,
    }
    if login_hint:
        par_body["login_hint"] = login_hint

    assert is_safe_url(par_url)
    dpop_authserver_nonce, resp = auth_server_post(
        authserver_url=authserver_url,
        client_id=client_id,
        client_secret_jwk=client_secret_jwk,
        dpop_private_jwk=dpop_private_jwk,
        dpop_authserver_nonce="",
        post_url=par_url,
        post_data=par_body,
    )

    return pkce_verifier, state, dpop_authserver_nonce, resp


def initial_token_request(
    auth_request, code, client_id, redirect_uri, client_secret_jwk
):
    """Exchange authorization code for tokens. Returns (token_body, dpop_nonce)."""
    authserver_url = auth_request["authserver_iss"]
    authserver_meta = fetch_authserver_meta(authserver_url)

    token_url = authserver_meta["token_endpoint"]
    dpop_private_jwk = JsonWebKey.import_key(
        json.loads(auth_request["dpop_private_jwk"])
    )

    params = {
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": auth_request["pkce_verifier"],
    }

    assert is_safe_url(token_url)
    dpop_authserver_nonce, resp = auth_server_post(
        authserver_url=authserver_url,
        client_id=client_id,
        client_secret_jwk=client_secret_jwk,
        dpop_private_jwk=dpop_private_jwk,
        dpop_authserver_nonce=auth_request["dpop_authserver_nonce"],
        post_url=token_url,
        post_data=params,
    )

    resp.raise_for_status()
    return resp.json(), dpop_authserver_nonce


def refresh_token_request(user, client_id, client_secret_jwk):
    """Refresh an access token. Returns (token_body, dpop_nonce)."""
    authserver_url = user["authserver_iss"]
    authserver_meta = fetch_authserver_meta(authserver_url)

    token_url = authserver_meta["token_endpoint"]
    dpop_private_jwk = JsonWebKey.import_key(json.loads(user["dpop_private_jwk"]))

    params = {
        "grant_type": "refresh_token",
        "refresh_token": user["refresh_token"],
    }

    assert is_safe_url(token_url)
    dpop_authserver_nonce, resp = auth_server_post(
        authserver_url=authserver_url,
        client_id=client_id,
        client_secret_jwk=client_secret_jwk,
        dpop_private_jwk=dpop_private_jwk,
        dpop_authserver_nonce=user["dpop_authserver_nonce"],
        post_url=token_url,
        post_data=params,
    )

    resp.raise_for_status()
    return resp.json(), dpop_authserver_nonce


def revoke_token_request(user, client_id, client_secret_jwk):
    """Revoke access and refresh tokens."""
    authserver_url = user["authserver_iss"]
    authserver_meta = fetch_authserver_meta(authserver_url)

    dpop_private_jwk = JsonWebKey.import_key(json.loads(user["dpop_private_jwk"]))
    dpop_authserver_nonce = user["dpop_authserver_nonce"]

    revoke_url = authserver_meta.get("revocation_endpoint")
    if not revoke_url:
        return

    assert is_safe_url(revoke_url)
    for token_type in ["access_token", "refresh_token"]:
        dpop_authserver_nonce, resp = auth_server_post(
            authserver_url=authserver_url,
            client_id=client_id,
            client_secret_jwk=client_secret_jwk,
            dpop_private_jwk=dpop_private_jwk,
            dpop_authserver_nonce=dpop_authserver_nonce,
            post_url=revoke_url,
            post_data={
                "token": user[token_type],
                "token_type_hint": token_type,
            },
        )
        resp.raise_for_status()


def pds_authed_req(method, url, user, db, body=None):
    """Make an authenticated request to a user's PDS with DPoP."""
    dpop_private_jwk = JsonWebKey.import_key(json.loads(user["dpop_private_jwk"]))
    dpop_pds_nonce = user["dpop_pds_nonce"] or ""
    access_token = user["access_token"]

    resp = None
    for _ in range(2):
        dpop_pub_jwk = json.loads(dpop_private_jwk.as_json(is_private=False))
        dpop_body = {
            "iat": int(time.time()),
            "exp": int(time.time()) + 10,
            "jti": generate_token(),
            "htm": method,
            "htu": url,
            "ath": create_s256_code_challenge(access_token),
        }
        if dpop_pds_nonce:
            dpop_body["nonce"] = dpop_pds_nonce
        dpop_jwt = jwt.encode(
            {"typ": "dpop+jwt", "alg": "ES256", "jwk": dpop_pub_jwk},
            dpop_body,
            dpop_private_jwk,
        ).decode("utf-8")

        with hardened_http.get_session() as sess:
            if method == "GET":
                resp = sess.get(
                    url,
                    headers={"Authorization": f"DPoP {access_token}", "DPoP": dpop_jwt},
                )
            else:
                resp = sess.post(
                    url,
                    headers={"Authorization": f"DPoP {access_token}", "DPoP": dpop_jwt},
                    json=body,
                )

        if is_use_dpop_nonce_error_response(resp):
            dpop_pds_nonce = resp.headers["DPoP-Nonce"]
            cur = db.cursor()
            cur.execute(
                "UPDATE oauth_session SET dpop_pds_nonce = ? WHERE did = ?;",
                [dpop_pds_nonce, user["did"]],
            )
            db.commit()
            cur.close()
            continue
        break

    return resp
