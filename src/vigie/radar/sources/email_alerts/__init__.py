"""Source ``email_alerts`` : offres lues dans les alertes email (FT, APEC, WTTJ, LinkedIn).

Seuls les emails dont l'expéditeur est dans la liste blanche sont lus. Les liens ne sont **jamais**
ouverts. Un message n'est marqué « traité » (table ``processed_emails``) qu'une fois ses offres
remises au pipeline ; un message dont aucune offre n'a pu être extraite n'est pas marqué (pour être
relu après correction du parseur), est signalé par un avertissement (expéditeur et date, jamais le
contenu) et compte une erreur.
"""

import email
import email.policy
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

from pydantic import ValidationError

from vigie.radar.config import EmailAlertsConfig
from vigie.radar.models import ContractType, Offer, SearchQuery
from vigie.radar.normalize import contract_from_title, parse_contract_type, parse_location
from vigie.radar.sources.email_alerts import apec, france_travail, linkedin, wttj
from vigie.radar.sources.email_alerts.mailbox import ImapMailbox, Mailbox, RawMessage
from vigie.radar.sources.email_alerts.parsing import SenderSpec, extract_cards
from vigie.radar.store import Store
from vigie.settings import MissingSettingsError, Settings

logger = logging.getLogger(__name__)

NAME = "email_alerts"
SPECS: dict[str, SenderSpec] = {
    spec.key: spec for spec in (france_travail.SPEC, apec.SPEC, wttj.SPEC, linkedin.SPEC)
}


def match_sender(address: str, allowed: dict[str, list[str]]) -> str | None:
    """Clé d'expéditeur dont un motif correspond à l'adresse.

    ``@domaine`` accepte le domaine et ses sous-domaines (``a@mail.domaine``), pas ``x-domaine`` ;
    un motif complet (``alertes@domaine``) doit être identique. L'en-tête From n'authentifie pas
    l'expéditeur : la liste blanche est un filtre, pas une garantie.
    """
    address = address.lower().strip()
    domain = address.rpartition("@")[2]
    for key, patterns in allowed.items():
        for pattern in patterns:
            pattern = pattern.lower().strip()
            if pattern.startswith("@"):
                wanted = pattern[1:]
                if domain == wanted or domain.endswith("." + wanted):
                    return key
            elif address == pattern:
                return key
    return None


def _html_body(raw: bytes) -> str:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    body = message.get_body(preferencelist=("html",))
    return str(body.get_content()) if body is not None else ""


class EmailAlertsSource:
    name = NAME

    def __init__(
        self,
        settings: Settings,
        config: EmailAlertsConfig,
        *,
        mailbox: Mailbox | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        settings.require(["imap"])
        self._allowed = config.allowed_senders
        self._now = now
        if mailbox is None:
            if (
                settings.imap_host is None
                or settings.imap_user is None
                or settings.imap_password is None
                or settings.imap_folder is None
            ):  # pragma: no cover (déjà garanti par require)
                raise MissingSettingsError(settings.missing(["imap"]))
            mailbox = ImapMailbox(
                settings.imap_host, settings.imap_user, settings.imap_password, settings.imap_folder
            )
        self._mailbox = mailbox
        self._store: Store | None = None
        self._offers: list[Offer] = []
        self._done = False
        self.skipped = 0

    def attach_store(self, store: Store) -> None:
        self._store = store

    def _skip(self, message_id: str, sender: str) -> bool:
        if match_sender(sender, self._allowed) is None:
            return True  # expéditeur hors liste blanche : ignoré, corps jamais lu
        return self._store is not None and self._store.email_processed(message_id)

    @staticmethod
    def _contract(label: str | None, title: str) -> ContractType:
        """Libellé de contrat de l'email d'abord, sinon un type explicite dans le titre."""
        if label:
            parsed = ContractType(parse_contract_type(label))
            if parsed is not ContractType.AUTRE:
                return parsed
        return ContractType(contract_from_title(title) or "AUTRE")

    def _offers_from(self, message: RawMessage, sender_key: str) -> list[Offer]:
        spec = SPECS.get(sender_key)
        if spec is None:
            return []
        fetched_at = self._now()
        offers: dict[str, Offer] = {}
        for card in extract_cards(_html_body(message.raw), spec):
            label, postal, department = parse_location(card.location)
            try:
                offer = Offer(
                    source=f"email:{sender_key}",
                    source_id=card.key,
                    title=card.title,
                    company=card.company,
                    location_label=label,
                    postal_code=postal,
                    department=department,
                    contract_type=self._contract(card.contract, card.title),
                    description=card.description,
                    url=card.url,
                    published_at=message.date,
                    fetched_at=fetched_at,
                    # Ni le corps ni l'adresse de l'utilisateur : seulement de quoi déboguer.
                    raw={
                        "sender_key": sender_key,
                        "message_id": message.message_id,
                        "link": card.url,
                    },
                )
            except ValidationError:
                self.skipped += 1
                continue
            offers.setdefault(offer.uid, offer)
        return list(offers.values())

    def fetch(self, query: SearchQuery, since: datetime) -> Iterator[Offer]:
        """Les offres ne dépendent pas de la recherche : la boîte n'est lue qu'une fois par run."""
        if self._done:
            yield from self._offers
            return
        for message in self._mailbox.messages(since, self._skip):
            sender_key = match_sender(message.from_address, self._allowed)
            if sender_key is None:  # pragma: no cover (déjà filtré par skip)
                continue
            try:
                offers = self._offers_from(message, sender_key)
            except Exception:  # corps illisible ou parseur cassé : on signale et on continue
                offers = []
            if not offers:
                logger.warning(
                    "Alerte email sans offre extraite : expéditeur=%s date=%s",
                    sender_key,
                    message.date.date().isoformat(),
                )
                self.skipped += 1
                continue
            self._offers.extend(offers)
            yield from offers
            if self._store is not None:
                self._store.mark_email_processed(
                    message.message_id, sender_key, self._now(), len(offers)
                )
        self._done = True
