import time
from concurrent.futures import ThreadPoolExecutor

import requests

CONSTELLATION_URL = "https://constellation.microcosm.blue"
SLINGSHOT_URL = "https://slingshot.microcosm.blue"
UFOS_API_URL = "https://ufos-api.microcosm.blue"

# Simple timed caches
_identity_cache: dict[str, tuple[tuple[str | None, str | None], float]] = {}
_identity_ttl = 3600
_profile_cache: dict[str, tuple[dict[str, str | None], float]] = {}
_profile_ttl = 3600
_recent_bites_cache: tuple[list[dict[str, str | None]], float] | None = None
_recent_bites_ttl = 60


def resolve_did(identifier: str) -> str | None:
    """Resolve a handle to a DID via Slingshot. Returns the DID, or None if resolution fails."""
    if identifier.startswith("did:"):
        return identifier
    try:
        resp = requests.get(
            f"{SLINGSHOT_URL}/xrpc/com.atproto.identity.resolveHandle",
            params={"handle": identifier},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("did")
    except (requests.RequestException, ValueError):
        return None


def resolve_identity(did: str) -> tuple[str | None, str | None]:
    """Resolve a DID to its handle and PDS URL via Slingshot."""
    now = time.time()
    if did in _identity_cache:
        result, ts = _identity_cache[did]
        if now - ts < _identity_ttl:
            return result

    try:
        resp = requests.get(
            f"{SLINGSHOT_URL}/xrpc/blue.microcosm.identity.resolveMiniDoc",
            params={"identifier": did},
            timeout=5,
        )
        if resp.status_code != 200:
            _identity_cache[did] = ((None, None), now)
            return None, None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None, None

    handle = data.get("handle")
    pds_url = data.get("pds")

    _identity_cache[did] = ((handle, pds_url), now)
    return handle, pds_url


def fetch_profile(did: str, pds_url: str) -> dict[str, str | None]:
    """Fetch a user's Bluesky profile via Slingshot."""
    now = time.time()
    if did in _profile_cache:
        result, ts = _profile_cache[did]
        if now - ts < _profile_ttl:
            return result

    try:
        resp = requests.get(
            f"{SLINGSHOT_URL}/xrpc/com.atproto.repo.getRecord",
            params={
                "repo": did,
                "collection": "app.bsky.actor.profile",
                "rkey": "self",
            },
            timeout=5,
        )
        if resp.status_code != 200:
            return {}
        value = resp.json().get("value", {})
    except requests.RequestException, ValueError:
        return {}

    avatar_url = None
    avatar_blob_url = None
    avatar = value.get("avatar")
    if avatar and isinstance(avatar, dict):
        cid = avatar.get("ref", {}).get("$link")
        if cid:
            avatar_url = f"https://cdn.bsky.app/img/avatar/plain/{did}/{cid}@webp"
            avatar_blob_url = (
                f"{pds_url}/xrpc/com.atproto.sync.getBlob?did={did}&cid={cid}"
            )

    profile = {
        "display_name": value.get("displayName"),
        "description": value.get("description"),
        "pronouns": value.get("pronouns"),
        "avatar_url": avatar_url,
        "avatar_blob_url": avatar_blob_url,
    }
    _profile_cache[did] = (profile, now)
    return profile


def fetch_recent_bites(limit: int = 5) -> list[dict[str, str | None]]:
    """Fetch the most recent bites network-wide from UFOs."""
    global _recent_bites_cache
    now = time.time()
    if _recent_bites_cache is not None:
        cached, ts = _recent_bites_cache
        if now - ts < _recent_bites_ttl:
            return cached[:limit]

    try:
        resp = requests.get(
            f"{UFOS_API_URL}/records",
            params={"collection": "blue.morsels.bite", "limit": limit},
            timeout=5,
        )
        if resp.status_code != 200:
            return []
        raw = resp.json()[:limit]
    except requests.RequestException, ValueError:
        return []

    # Resolve all identities in parallel
    dids = [item.get("did", "") for item in raw]
    unique_dids = list(set(d for d in dids if d))
    with ThreadPoolExecutor(max_workers=5) as pool:
        identity_results = dict(zip(unique_dids, pool.map(resolve_identity, unique_dids)))

    # Fetch profiles in parallel too (for avatar URLs)
    def _fetch_profile_for_did(did: str) -> dict[str, str | None]:
        handle, pds_url = identity_results.get(did, (None, None))
        if pds_url:
            return fetch_profile(did, pds_url)
        return {}

    with ThreadPoolExecutor(max_workers=5) as pool:
        profile_results = dict(zip(unique_dids, pool.map(_fetch_profile_for_did, unique_dids)))

    bites = []
    for item in raw:
        record = item.get("record", {})
        did = item.get("did", "")
        handle, _ = identity_results.get(did, (None, None))
        profile = profile_results.get(did, {})
        try:
            bites.append({
                "did": did,
                "handle": handle,
                "rkey": item.get("rkey", ""),
                "title": record["title"],
                "content": record["content"],
                "created_at": record.get("createdAt", ""),
                "avatar_url": profile.get("avatar_url") or f"/avatar/{did}",
            })
        except (KeyError, TypeError):
            continue

    _recent_bites_cache = (bites, now)
    return bites


def fetch_replies(did: str, rkey: str) -> list[dict[str, str]]:
    """Fetch reply backlinks from Constellation."""
    at_uri = f"at://{did}/blue.morsels.bite/{rkey}"

    try:
        resp = requests.get(
            f"{CONSTELLATION_URL}/xrpc/blue.microcosm.links.getBacklinks",
            params={
                "subject": at_uri,
                "source": "blue.morsels.reply:subject.uri",
                "limit": 100,
            },
            timeout=5,
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("records", [])
    except requests.RequestException, ValueError:
        return []


def hydrate_replies(records: list[dict[str, str]]) -> list[dict[str, str | None]]:
    """Fetch reply record contents from Slingshot."""
    replies: list[dict[str, str | None]] = []
    for record in records:
        did = record.get("did")
        rkey = record.get("rkey")
        if not did or not rkey:
            continue

        try:
            resp = requests.get(
                f"{SLINGSHOT_URL}/xrpc/com.atproto.repo.getRecord",
                params={
                    "repo": did,
                    "collection": "blue.morsels.reply",
                    "rkey": rkey,
                },
                timeout=5,
            )
            if resp.status_code != 200:
                continue
            value = resp.json().get("value", {})
        except requests.RequestException, ValueError:
            continue

        handle, _ = resolve_identity(did)

        replies.append(
            {
                "did": did,
                "handle": handle,
                "rkey": rkey,
                "text": value.get("text", ""),
                "created_at": value.get("createdAt", ""),
            }
        )

    return replies
