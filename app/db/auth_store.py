from __future__ import annotations
import json
from typing import Optional

from sqlalchemy import delete, update
from sqlmodel import Session, select

from core.auth import AccountTarget, StoredCredential
from musizeer_sdk.auth import AccountModel
from . import credential_cipher
from .models import NO_USER, ProviderCredential, _now
from .upsert import upsert


def _what(row: ProviderCredential) -> str:
    """Names a stored credential for a warning or an error: what kind, of which provider, whose.
    Never a value."""
    owner = "shared" if row.user_id is None else f"personal, user {row.user_id}"
    return f"The {row.credential_type} credential of provider '{row.provider}' ({owner})"


def _data(row: ProviderCredential) -> dict | None:
    """The credential's data, or None when the configured keys cannot open it (it is then, for
    every reader, a credential that is not there: ``credential_cipher.unseal`` says so once)."""
    text = credential_cipher.unseal(row.data, _what(row))
    return json.loads(text) if text is not None else None


def _stored(row: ProviderCredential) -> StoredCredential | None:
    data = _data(row)
    # ``raw`` is the text as it is stored (sealed): what a compare-and-swap checks.
    return StoredCredential(id=row.id, user_id=row.user_id, data=data, raw=row.data) if data is not None else None


class CredentialStore:
    """
    Unico punto di accesso alle credenziali dei provider.
    AuthManager dipende da questa classe — non accede mai al DB direttamente.

    Every credential is stored SEALED (``db/credential_cipher.py``, ``CREDENTIALS_KEY``): a write
    seals, a read opens. One that the configured keys cannot open (the key is gone or wrong, the
    text was altered, or it is in clear) is, for every reader, a credential that is not there: the
    account is "not connected", the app has no OAuth client, the API key is not set. Connecting it
    again replaces it (so does registering an account of an external utility while the object that
    holds the registered ones cannot be read: it starts again from that account, as if it were
    the first). Disconnecting one that cannot be read removes it.

    The ONE routing rule (``musizeer_sdk.auth.AccountModel``), for what a provider's
    calls READ (an OAuth2 token, a login):

    - ``PERSONAL``: the user's own row, never the shared one;
    - ``PERSONAL_WITH_SHARED``: the user's own row, else the shared one;
    - ``SYSTEM``: the shared row (``user_id`` NULL);
    - ``NONE``: nothing to read.

    A store built without a user (host jobs) only sees the shared row. A store
    built ``own_only`` (an action on the user's own account: taking a like
    back, importing their likes) never sees it, whatever the model. What is
    always the instance's (API key, OAuth client, app-only token, external
    utility accounts) is read and written as shared, whoever holds the store.
    WRITES never route: the caller names the ``AccountTarget``.

    ``instance_owner``: the principal IS the instance (authentication off: the
    synthetic user is the only person there is), so "my own account" and "the
    instance's shared account" are the same account. Its personal account is
    then the shared row: read there, written there (``AccountTarget.PERSONAL``
    lands on the shared row), seen by an ``own_only`` store (the shared row is
    its own account; an app-only token still is not). It is never set by a
    caller that does not know the mode: ``services.credentials`` decides it, once.
    """

    def __init__(
        self, session: Session, user_id: Optional[str] = None, *, own_only: bool = False, instance_owner: bool = False,
    ) -> None:
        self._session = session
        self._user_id = user_id
        self._own_only = own_only
        self._instance_owner = instance_owner and user_id is not None

    @property
    def session(self) -> Session:
        """La `Session` con cui questo store è stato costruito. Espone quello
        che i chiamanti che possiedono solo uno store (es. `AuthManager`, che
        non ha mai avuto una `Session` propria) hanno bisogno per costruire un
        `SettingsRepository`: la config di provider non passa da questa
        classe, dedicata ai soli segreti."""
        return self._session

    @property
    def instance_owner(self) -> bool:
        """The principal is the instance itself (authentication off): its own
        account is the shared one, and it has no sign-in of its own."""
        return self._instance_owner

    @property
    def own_only(self) -> bool:
        """The store acts on the user's own account only: no shared account,
        no app-only token (``AuthManager`` reads it)."""
        return self._own_only

    # ------------------------------------------------------------------
    # Per-account credentials: OAuth2 token, login. Read by model, written by target.
    # ------------------------------------------------------------------

    def oauth2_token_candidates(self, provider: str, model: AccountModel) -> list[StoredCredential]:
        """The OAuth2 tokens ``model`` lets a call of ``provider`` use, in the
        order they are tried."""
        return self._candidates("oauth2_token", provider, model)

    def credentials_candidates(self, provider: str, model: AccountModel) -> list[StoredCredential]:
        return self._candidates("credentials", provider, model)

    def own_oauth2_token(self, provider: str) -> StoredCredential | None:
        return self._exact("oauth2_token", provider, self._own_owner()) if self._user_id else None

    def shared_oauth2_token(self, provider: str) -> StoredCredential | None:
        return self._exact("oauth2_token", provider, None)

    def own_credentials(self, provider: str) -> StoredCredential | None:
        return self._exact("credentials", provider, self._own_owner()) if self._user_id else None

    def shared_credentials(self, provider: str) -> StoredCredential | None:
        return self._exact("credentials", provider, None)

    def save_oauth2_token(
        self, provider: str, token_data: dict, target: AccountTarget, *, external_id: str | None = None,
    ) -> None:
        self._upsert("oauth2_token", provider, token_data, self._owner(target), external_id)

    def save_credentials(
        self, provider: str, data: dict, target: AccountTarget, *, external_id: str | None = None,
    ) -> None:
        self._upsert("credentials", provider, data, self._owner(target), external_id)

    def own_external_id(self, provider: str) -> str | None:
        """The id the provider gave the user's own account (OAuth2 or login), if it was asked.
        The instance has none: that id is what a person signs in with, and the shared account is nobody's."""
        if self._user_id is None or self._instance_owner:
            return None
        row = self._session.exec(
            select(ProviderCredential.external_id).where(
                ProviderCredential.provider == provider,
                ProviderCredential.user_id == self._user_id,
                ProviderCredential.external_id.is_not(None),  # type: ignore[union-attr]
            )
        ).first()
        return row

    def linked_user_id(self, provider: str, external_id: str) -> str | None:
        """The user whose account of ``provider`` has this id, if any (the id is unique per provider)."""
        return self._session.exec(
            select(ProviderCredential.user_id).where(
                ProviderCredential.provider == provider,
                ProviderCredential.external_id == external_id,
            )
        ).first()

    def delete_oauth2_token(self, provider: str, target: AccountTarget) -> None:
        self._delete("oauth2_token", provider, self._owner(target))

    def delete_credentials(self, provider: str, target: AccountTarget) -> None:
        self._delete("credentials", provider, self._owner(target))

    def delete_all_personal(self) -> int:
        """Every account the user connected, in one statement; how many. Never the instance's
        shared accounts, even for the principal that is the instance (they are managed one by one)."""
        if self._user_id is None or self._instance_owner:
            return 0
        removed = self._session.exec(delete(ProviderCredential).where(ProviderCredential.user_id == self._user_id))
        self._session.commit()
        return removed.rowcount

    def replace_oauth2_token(self, provider: str, current: StoredCredential, token_data: dict) -> bool:
        """Stores the refreshed ``token_data`` over ``current`` ONLY if that
        row is still exactly what was read (compare-and-swap). A token is
        refreshed over the network, away from the database: meanwhile the
        account may have been disconnected (the refresh must not bring it
        back), reconnected (it must not overwrite the new account with the
        old one) or refreshed by another worker (whose token stays). False
        when the row changed or is gone: the refreshed token is dropped."""
        replaced = self._session.exec(
            update(ProviderCredential)
            .where(ProviderCredential.id == current.id, ProviderCredential.data == current.raw)
            .values(data=credential_cipher.seal(json.dumps(token_data)), updated_at=_now())
        )
        self._session.commit()
        return replaced.rowcount == 1

    # ------------------------------------------------------------------
    # The instance's own: OAuth2 app-only token and client, API key, external utility
    # ------------------------------------------------------------------

    def get_oauth2_app_token(self, provider: str) -> dict | None:
        return self._shared_data("oauth2_app_token", provider)

    def save_oauth2_app_token(self, provider: str, token_data: dict) -> None:
        self._upsert("oauth2_app_token", provider, token_data, None)

    def get_oauth2_client(self, provider: str) -> dict | None:
        return self._shared_data("oauth2_client", provider)

    def get_oauth2_client_id(self, provider: str) -> str | None:
        client = self.get_oauth2_client(provider)
        return client.get("client_id") if client else None

    def save_oauth2_client(self, provider: str, client_id: str, client_secret: str) -> None:
        self._upsert("oauth2_client", provider, {"client_id": client_id, "client_secret": client_secret}, None)

    def delete_oauth2_client(self, provider: str) -> None:
        """Without the client the shared token cannot be refreshed: it goes too."""
        self._delete("oauth2_client", provider, None)
        self._delete("oauth2_token", provider, None)

    def get_api_key(self, provider: str) -> str | None:
        data = self._shared_data("api_key", provider)
        return data.get("api_key") if data else None

    def save_api_key(self, provider: str, api_key: str) -> None:
        self._upsert("api_key", provider, {"api_key": api_key}, None)

    def delete_api_key(self, provider: str) -> None:
        self._delete("api_key", provider, None)

    def get_external_utility_accounts(self, provider: str, credential_type: str) -> dict:
        """Ritorna il dict di account: {account_name: {...payload...}}."""
        return self._shared_data(credential_type, provider) or {}

    def save_external_utility_account(
        self,
        provider: str,
        credential_type: str,
        account_name: str,
        payload: dict,
    ) -> None:
        accounts = self.get_external_utility_accounts(provider, credential_type)
        accounts[account_name] = payload
        self._upsert(credential_type, provider, accounts, None)

    def delete_external_utility_account(
        self,
        provider: str,
        credential_type: str,
        account_name: str,
    ) -> bool:
        accounts = self.get_external_utility_accounts(provider, credential_type)
        if account_name not in accounts:
            return False
        del accounts[account_name]
        if accounts:
            self._upsert(credential_type, provider, accounts, None)
        else:
            self._delete(credential_type, provider, None)
        return True

    # ------------------------------------------------------------------
    # Helpers interni
    # ------------------------------------------------------------------

    def _own_owner(self) -> Optional[str]:
        """The ``user_id`` of the row that holds the principal's own account: theirs, or None
        (the shared row) when the principal is the instance. Only with a principal."""
        return None if self._instance_owner else self._user_id

    def _owner(self, target: AccountTarget) -> Optional[str]:
        """The ``user_id`` a write for ``target`` goes to (None = shared)."""
        if target == AccountTarget.SHARED:
            return None
        if self._user_id is None:
            raise ValueError("a personal account needs a user")
        return self._own_owner()

    def _owners(self, model: AccountModel) -> list[Optional[str]]:
        """Whose rows a read for ``model`` looks at, in order (None = shared)."""
        owners: list[Optional[str]] = []
        if model.has_personal and self._user_id is not None:
            owners.append(self._own_owner())
        if model.has_shared and not self._own_only and None not in owners:
            owners.append(None)
        return owners

    def _candidates(self, credential_type: str, provider: str, model: AccountModel) -> list[StoredCredential]:
        found = (self._exact(credential_type, provider, owner) for owner in self._owners(model))
        return [credential for credential in found if credential is not None]

    def _row(self, credential_type: str, provider: str, user_id: Optional[str]) -> ProviderCredential | None:
        statement = select(ProviderCredential).where(
            ProviderCredential.provider == provider,
            ProviderCredential.credential_type == credential_type,
        )
        if user_id is None:
            statement = statement.where(ProviderCredential.user_id.is_(None))  # type: ignore[union-attr]
        else:
            statement = statement.where(ProviderCredential.user_id == user_id)
        return self._session.exec(statement).first()

    def _exact(self, credential_type: str, provider: str, user_id: Optional[str]) -> StoredCredential | None:
        row = self._row(credential_type, provider, user_id)
        return _stored(row) if row else None

    def _shared_data(self, credential_type: str, provider: str) -> dict | None:
        row = self._row(credential_type, provider, None)
        return _data(row) if row else None

    def _upsert(
        self, credential_type: str, provider: str, data: dict, user_id: Optional[str], external_id: str | None = None,
    ) -> None:
        # ``INSERT ... ON CONFLICT``: two writers of the same credential (two
        # workers refreshing one token) leave one row. The id of the account on
        # its provider is written when given and otherwise left as it is; a
        # shared row never carries one (it is what a person signs in with, and
        # it is unique per provider: it would keep the person from linking it).
        if user_id is None:
            external_id = None
        upsert(
            self._session,
            ProviderCredential(
                user_id=user_id,
                provider=provider,
                credential_type=credential_type,
                data=credential_cipher.seal(json.dumps(data)),
                external_id=external_id,
            ),
            conflict=("provider", "credential_type") if user_id is None else ("user_id", "provider", "credential_type"),
            where=NO_USER if user_id is None else None,
            update=("data", "updated_at", "external_id") if external_id is not None else ("data", "updated_at"),
        )
        self._session.commit()

    def _delete(self, credential_type: str, provider: str, user_id: Optional[str]) -> None:
        # A statement, not a loaded row: deleting what another writer just
        # deleted is not an error.
        statement = delete(ProviderCredential).where(
            ProviderCredential.provider == provider,
            ProviderCredential.credential_type == credential_type,
            ProviderCredential.user_id.is_(None) if user_id is None else ProviderCredential.user_id == user_id,  # type: ignore[union-attr]
        )
        self._session.exec(statement)
        self._session.commit()

    def rollback(self) -> None:
        """Rollback della transazione corrente per ripristinare la sessione dopo un errore."""
        self._session.rollback()
