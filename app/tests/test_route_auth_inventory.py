"""
Inventario delle rotte e della loro policy di accesso. Ogni APIRoute
dell'app deve comparire qui con la policy attesa, e la policy dichiarata
deve corrispondere alle dipendenze reali della rotta: una rotta nuova senza
voce, o una rotta la cui autenticazione cambia, fa fallire il test. Cosi'
nessuna rotta arriva in produzione senza autenticazione per sbaglio.

Policy, dalla più restrittiva:
- admin    → ``require_admin`` (AdminUserDep)
- stream   → ``get_stream_user`` (cookie di stream o bearer)
- admin    → anche ``require_stream_admin``: cookie di stream o bearer, ruolo admin
- user     → ``get_current_user`` (CurrentUserDep)
- optional → ``get_optional_user`` (anonimo ammesso, utente attribuito se c'è)
- token    → nessuna dipendenza utente; la rotta verifica un token condiviso
             nel corpo (``secrets.compare_digest``, fail-closed)
- public   → nessuna autenticazione, per scelta
"""
from __future__ import annotations

from routes import deps
from tests.route_support import api_routes

_DEPENDENCY_POLICY = {
    deps.require_admin: "admin",
    deps.require_stream_admin: "admin",
    deps.get_stream_user: "stream",
    deps.get_current_user: "user",
    deps.get_optional_user: "optional",
}
_ORDER = ("admin", "stream", "user", "optional")

EXPECTED: dict[tuple[str, str], str] = {
    # --- admin (routes/admin.py)
    ("GET", "/admin/users"): "admin",
    ("POST", "/admin/users"): "admin",
    ("PATCH", "/admin/users/{user_id}"): "admin",
    ("DELETE", "/admin/users/{user_id}"): "admin",
    ("GET", "/admin/users/{user_id}/download-policy"): "admin",
    ("PUT", "/admin/users/{user_id}/download-policy"): "admin",
    ("DELETE", "/admin/users/{user_id}/download-policy"): "admin",

    # --- download approval policy (routes/admin_download_requests.py, routes/download_requests.py)
    ("GET", "/admin/download-requests"): "admin",
    ("GET", "/admin/download-requests/pending-count"): "admin",
    ("POST", "/admin/download-requests/approve"): "admin",
    ("POST", "/admin/download-requests/deny"): "admin",
    ("GET", "/admin/download-policy"): "admin",
    ("PATCH", "/admin/download-policy"): "admin",
    ("GET", "/download-requests"): "user",
    ("POST", "/download-requests/{request_id}/cancel"): "user",

    # --- account (routes/users_auth.py)
    ("GET", "/auth/account/status"): "optional",
    ("POST", "/auth/account/register"): "public",
    ("POST", "/auth/account/login"): "public",
    ("GET", "/auth/account/login-providers"): "public",
    ("GET", "/auth/account/login/{provider}/start"): "public",
    ("POST", "/auth/account/login/{provider}/credentials"): "public",
    ("POST", "/auth/account/login/exchange"): "public",
    ("POST", "/auth/account/refresh"): "public",
    ("POST", "/auth/account/logout"): "public",
    ("GET", "/auth/account/me"): "user",
    ("POST", "/auth/account/stream-session"): "user",
    ("DELETE", "/auth/account/stream-session"): "public",

    # --- provider auth (routes/auth.py), montato su /auth e /api/auth
    **{
        (method, f"{prefix}{path}"): policy
        for prefix in ("/auth", "/api/auth")
        for method, path, policy in (
            ("GET", "/status", "public"),
            ("POST", "/login", "public"),
            ("GET", "/linked-accounts", "user"),
            ("GET", "/{provider}/authorize-url", "user"),
            ("GET", "/{provider}/start", "user"),
            ("GET", "/{provider}/callback", "public"),  # protetta dallo state token OAuth
            ("GET", "/{provider}/status", "user"),
            ("POST", "/{provider}/apikey", "admin"),
            ("POST", "/{provider}/client", "admin"),
            ("DELETE", "/{provider}/client", "admin"),
            ("POST", "/{provider}/credentials", "user"),
            ("POST", "/{provider}/test", "user"),
            ("DELETE", "/{provider}", "user"),
            ("DELETE", "/me/accounts", "user"),
        )
    },

    # --- external utility credentials (routes/external_utility.py), su / e /api
    **{
        (method, f"{prefix}{path}"): "admin"
        for prefix in ("", "/api")
        for method, path in (
            ("GET", "/credentials/{intake_name}_api_config"),
            ("PUT", "/credentials/{intake_name}_api_config"),
            ("GET", "/credentials/{intake_name}"),
            ("POST", "/credentials/{intake_name}/{account_name}"),
            ("DELETE", "/credentials/{intake_name}/{account_name}"),
        )
    },

    # --- delegation webhook (routes/delegation_webhook.py), su / e /api
    ("POST", "/delegation/webhook/{provider_name}"): "token",
    ("POST", "/api/delegation/webhook/{provider_name}"): "token",
    ("POST", "/delegation/register-webhook"): "admin",
    ("POST", "/api/delegation/register-webhook"): "admin",

    # --- search & detail pages (showcase pubblico, ricerca solo con login).
    # Every route that calls a provider is "optional" at least: anonymous is
    # welcome, and a signed-in viewer's calls use their own account of the
    # provider when its model has one (``routes.deps.get_credential_store``).
    ("GET", "/search"): "user",
    ("GET", "/track/{provider}/{id}"): "optional",  # pubblica; lo stato per-utente (preferiti) solo a chi è autenticato
    ("GET", "/track/{provider}/{id}/lyrics"): "optional",
    ("GET", "/track/{provider}/{id}/related"): "optional",  # pubblica; lo stato per-utente (preferiti) solo a chi è autenticato
    ("GET", "/track/{provider}/{id}/appears-on"): "optional",
    ("GET", "/album/{provider}/{id}"): "optional",  # pubblica; lo stato per-utente (preferiti) solo a chi è autenticato
    ("GET", "/album/{provider}/{id}/editions"): "optional",
    ("GET", "/artist/{provider}/{id}"): "optional",
    ("GET", "/artist/{provider}/{id}/albums"): "optional",  # pubblica; lo stato per-utente (preferiti) solo a chi è autenticato
    ("GET", "/artist/{provider}/{id}/top-tracks"): "optional",  # pubblica; lo stato per-utente (preferiti) solo a chi è autenticato
    ("GET", "/playlist/{provider}/{id}"): "optional",  # pubblica; lo stato per-utente (preferiti) solo a chi è autenticato
    ("GET", "/cover/provider/{provider_name}/{cover_id}"): "optional",
    ("GET", "/preview/{provider_name}/{track_id}"): "optional",  # le anteprime sono per tutti, ospiti compresi
    ("GET", "/discover/new-releases"): "optional",
    ("GET", "/discover/trending/albums"): "optional",
    ("GET", "/discover/trending/artists"): "optional",
    ("GET", "/discover/trending/tracks"): "optional",  # pubblica; lo stato per-utente (preferiti) solo a chi è autenticato

    # --- downloads (routes/content/*, routes/history.py)
    ("POST", "/download/track"): "user",
    ("POST", "/download/album"): "user",
    ("POST", "/download/artist"): "user",
    ("POST", "/download/playlist"): "user",
    ("POST", "/downloads/{track_id}/retry"): "user",
    ("POST", "/downloads/retry-failed"): "user",
    ("GET", "/downloads"): "user",
    ("GET", "/downloads/summary"): "user",

    # --- playback & streaming (routes/stream.py, routes/system/progress.py)
    ("GET", "/playback/{provider}/{provider_id}"): "optional",
    ("GET", "/stream/{track_id}"): "stream",
    ("GET", "/stream/provider/{provider_name}/{track_id}"): "stream",
    ("GET", "/progress"): "stream",

    # --- library (routes/library.py, routes/library_duplicates.py)
    # --- playlists of the user (routes/playlists.py): private, owner only
    ("GET", "/playlists"): "user",
    ("POST", "/playlists"): "user",
    ("POST", "/playlists/follow"): "user",
    ("GET", "/playlists/{playlist_id}"): "user",
    ("PATCH", "/playlists/{playlist_id}"): "user",
    ("DELETE", "/playlists/{playlist_id}"): "user",
    ("POST", "/playlists/{playlist_id}/tracks"): "user",
    ("PUT", "/playlists/{playlist_id}/tracks/order"): "user",
    ("DELETE", "/playlists/{playlist_id}/tracks/{track_id}"): "user",
    ("POST", "/playlists/{playlist_id}/duplicate"): "user",
    ("POST", "/playlists/{playlist_id}/refresh"): "user",

    ("GET", "/favorites"): "user",
    ("GET", "/favorites/{track_id}"): "user",
    ("PUT", "/favorites/{track_id}"): "user",
    ("DELETE", "/favorites/{track_id}"): "user",
    ("POST", "/favorites/batch"): "user",
    ("GET", "/favorites/elsewhere"): "user",
    ("DELETE", "/library/saved-tracks"): "user",
    ("POST", "/library/sync"): "user",
    ("GET", "/library/sync/status"): "user",
    ("POST", "/library/download-sync"): "user",
    ("GET", "/library/download-sync/status"): "user",
    ("GET", "/library/jobs"): "user",
    ("GET", "/library/duplicates"): "admin",
    ("POST", "/library/duplicates/{group_id}/resolve"): "admin",
    ("POST", "/library/duplicates/resolve-all"): "admin",
    ("POST", "/library/duplicates/{group_id}/dismiss"): "admin",
    ("GET", "/library/duplicates/members/{member_id}/stream"): "admin",  # require_stream_admin

    # --- settings & providers (routes/config.py)
    ("GET", "/providers/public"): "optional",
    ("GET", "/providers"): "user",
    ("DELETE", "/providers/{name}/accounts/{account_name}"): "admin",
    ("GET", "/settings/schema"): "user",
    ("GET", "/settings/values"): "user",
    ("PATCH", "/settings"): "user",
    ("GET", "/config/download-states"): "public",
    ("POST", "/config/verify-full-sync"): "admin",
    ("GET", "/config/verify-full-sync/{task_id}"): "admin",

    # --- provider sources (routes/sources.py)
    ("GET", "/sources"): "admin",
    ("POST", "/sources"): "admin",
    ("PATCH", "/sources/{source_id}"): "admin",
    ("DELETE", "/sources/{source_id}"): "admin",
    ("GET", "/sources/{source_id}/catalog"): "admin",
    ("GET", "/sources/providers/installed"): "admin",
    ("GET", "/sources/providers/gateway"): "admin",
    ("POST", "/sources/providers/install"): "admin",
    ("POST", "/sources/providers/{name}/update"): "admin",
    ("DELETE", "/sources/providers/{name}"): "admin",

    # --- system (routes/system/*)
    ("GET", "/health"): "public",
    ("GET", "/admin/updates"): "admin",
    ("GET", "/system/routines"): "admin",
    ("PATCH", "/system/routines/{key}"): "admin",
    ("POST", "/system/routines/{key}/run"): "admin",
    ("GET", "/openapi.yaml"): "public",
}


def _dependency_calls(dependant, found: set[str]) -> set[str]:
    for sub in dependant.dependencies:
        policy = _DEPENDENCY_POLICY.get(sub.call)
        if policy:
            found.add(policy)
        _dependency_calls(sub, found)
    return found


def _actual_policies() -> dict[tuple[str, str], str]:
    actual: dict[tuple[str, str], str] = {}
    for route in api_routes():
        found = _dependency_calls(route.dependant, set())
        policy = next((p for p in _ORDER if p in found), "none")
        for method in route.methods:
            actual[(method, route.path)] = policy
    return actual


def test_every_route_is_in_the_inventory():
    missing = sorted(set(_actual_policies()) - set(EXPECTED))
    assert not missing, f"rotte senza policy dichiarata in EXPECTED: {missing}"


def test_the_inventory_has_no_stale_routes():
    stale = sorted(set(EXPECTED) - set(_actual_policies()))
    assert not stale, f"voci di EXPECTED per rotte che non esistono più: {stale}"


def test_each_route_has_the_declared_policy():
    actual = _actual_policies()
    mismatches = []
    for key, expected in EXPECTED.items():
        got = actual.get(key)
        wanted = "none" if expected in ("public", "token") else expected
        if got is not None and got != wanted:
            mismatches.append(f"{key}: dichiarata {expected}, dipendenze {got}")
    assert not mismatches, "\n".join(mismatches)


def test_token_routes_check_a_shared_token_with_constant_time_compare():
    import inspect

    token_endpoints = {
        route.endpoint
        for route in api_routes()
        if any(EXPECTED.get((m, route.path)) == "token" for m in route.methods)
    }
    assert token_endpoints
    for endpoint in token_endpoints:
        source = inspect.getsource(endpoint)
        assert "compare_digest" in source, f"{endpoint.__name__} non confronta il token in tempo costante"
