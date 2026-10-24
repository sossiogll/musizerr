"""
Optional capabilities of a provider, by stable key.

A provider declares a capability with its ``supports_*`` method; the host
lists the declared ones (GET /providers/public), a setting names the one it
acts through and an OAuth2 provider maps them to the scopes they need
(``OAuth2Config.capability_scopes``). The UI words each key.

Some of them act on the USER's own account of the provider: the linked
accounts of a user (GET /auth/linked-accounts) say what each one enables.
"""
from __future__ import annotations

CAPABILITY_USER_LIBRARY = "user_library"
CAPABILITY_UNLIKE_SAVED_TRACK = "unlike_saved_track"
CAPABILITY_DISCOGRAPHY = "discography"
# A sync provider that takes the favorites of the user (``SyncProvider.supports_likes``).
CAPABILITY_PROPAGATE_FAVORITES = "propagate_favorites"
# The user's own account of the provider can sign them in (``BaseProvider.supports_login``).
CAPABILITY_LOGIN = "login"
# A metadata provider whose service hands out the tracks of a playlist only for
# the ones the connected account owns or collaborates on
# (``MetadataProvider.restricts_playlist_tracks``): the host says so before a
# playlist of another owner is followed (its tracks may come back unreadable,
# ``Playlist.tracks_unavailable``).
CAPABILITY_PLAYLIST_TRACKS_RESTRICTED = "playlist_tracks_restricted"
# A sync provider that keeps the user's playlists (``SyncProvider.supports_playlists``).
CAPABILITY_PROPAGATE_PLAYLISTS = "propagate_playlists"
