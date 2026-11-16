import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.channels.contracts import IncomingContact, IncomingMessage, OutgoingMessage
from app.models import ChannelAccount, ChannelAccountState, ChannelKind, User, UserStatus
from app.phones.service import PhoneService
from app.time_utils import validate_timezone


@dataclass(frozen=True)
class OnboardingResult:
    account: ChannelAccount
    replies: list[OutgoingMessage]
    handled: bool


class OnboardingService:
    def __init__(self, db: Session):
        self.db = db
        self.phones = PhoneService(db)

    def handle(self, account: ChannelAccount, message: IncomingMessage) -> OnboardingResult:
        if message.text and message.text.strip().casefold() == "/start":
            return self._start(account, message)
        if account.onboarding_state == ChannelAccountState.needs_name:
            return self._name(account, message)
        if account.onboarding_state == ChannelAccountState.needs_timezone:
            return self._timezone(account, message)
        if account.onboarding_state == ChannelAccountState.needs_phone_decision:
            return self._phone_decision(account, message)
        return OnboardingResult(account, [], False)

    def create_account(self, message: IncomingMessage) -> ChannelAccount:
        display_name = (message.display_name or "").strip()
        user = User(
            name=display_name or None,
            timezone=None,
            status=UserStatus.pending,
        )
        state = (
            ChannelAccountState.needs_timezone
            if display_name
            else ChannelAccountState.needs_name
        )
        account = ChannelAccount(
            user=user,
            channel=ChannelKind(message.channel),
            external_user_id=message.external_user_id,
            external_chat_id=message.external_chat_id,
            onboarding_state=state,
        )
        self.db.add(account)
        self.db.flush()
        return account

    def _start(self, account: ChannelAccount, message: IncomingMessage) -> OnboardingResult:
        if account.onboarding_state == ChannelAccountState.completed:
            return OnboardingResult(
                account, [self._reply(account, "Welcome back! How can I help?")], True
            )
        return OnboardingResult(account, [self._prompt(account)], True)

    def _name(self, account: ChannelAccount, message: IncomingMessage) -> OnboardingResult:
        name = (message.text or "").strip()
        if not name or name.startswith("/"):
            return OnboardingResult(
                account, [self._reply(account, "Please send your full name.")], True
            )
        account.user.name = name[:200]
        account.onboarding_state = ChannelAccountState.needs_timezone
        self.db.flush()
        return OnboardingResult(
            account,
            [self._timezone_prompt(account)],
            True,
        )

    def _timezone(self, account: ChannelAccount, message: IncomingMessage) -> OnboardingResult:
        timezone_name = (message.text or "").strip()
        try:
            validate_timezone(timezone_name)
        except ValueError:
            return OnboardingResult(
                account,
                [
                    self._reply(
                        account,
                        "That timezone is invalid. Please send an IANA timezone "
                        "such as Europe/London.",
                    )
                ],
                True,
            )
        account.user.timezone = timezone_name
        account.user.status = UserStatus.active
        account.onboarding_state = ChannelAccountState.needs_phone_decision
        self.db.flush()
        return OnboardingResult(
            account,
            [
                self._reply(
                    account,
                    "Would you like to link your phone for live calls? "
                    "Share your contact or tap Skip for now.",
                    request_phone=True,
                )
            ],
            True,
        )

    def _phone_decision(
        self, account: ChannelAccount, message: IncomingMessage
    ) -> OnboardingResult:
        if message.contact is not None:
            try:
                number = self.phones.normalize_contact(
                    sender_id=message.external_user_id,
                    owner_external_id=message.contact.owner_external_id,
                    raw_number=message.contact.phone_number,
                )
                self.phones.link(account.user.id, number)
            except ValueError as exc:
                return OnboardingResult(account, [self._reply(account, str(exc))], True)
            account.onboarding_state = ChannelAccountState.completed
            self.db.flush()
            return OnboardingResult(
                account,
                [
                    self._reply(
                        account,
                        "Your phone is linked. You can now use the agent here and on live calls.",
                    )
                ],
                True,
            )

        if (message.text or "").strip().casefold() in {"skip", "skip for now", "no", "not now"}:
            account.onboarding_state = ChannelAccountState.completed
            self.db.flush()
            return OnboardingResult(
                account,
                [
                    self._reply(
                        account,
                        "No problem. You can use the agent here; live calls need a linked phone.",
                    )
                ],
                True,
            )
        return OnboardingResult(
            account,
            [
                self._reply(
                    account,
                    "Please share your Telegram contact or tap Skip for now.",
                    request_phone=True,
                )
            ],
            True,
        )

    @staticmethod
    def _reply(
        account: ChannelAccount, text: str, *, request_phone: bool = False
    ) -> OutgoingMessage:
        return OutgoingMessage(
            channel=account.channel.value,
            external_chat_id=account.external_chat_id,
            text=text,
            request_phone=request_phone,
        )

    def _prompt(self, account: ChannelAccount) -> OutgoingMessage:
        if account.onboarding_state == ChannelAccountState.needs_name:
            return self._reply(account, "Welcome! What is your full name?")
        if account.onboarding_state == ChannelAccountState.needs_timezone:
            return self._timezone_prompt(account)
        return self._reply(
            account,
            "Would you like to link your phone for live calls? "
            "Share your contact or tap Skip for now.",
            request_phone=True,
        )

    def _timezone_prompt(self, account: ChannelAccount) -> OutgoingMessage:
        name = (account.user.name or "").strip()
        greeting = f"Hi {name}! Welcome to Memoray. " if name else "Welcome to Memoray. "
        return self._reply(
            account,
            greeting + "What is your IANA timezone? For example: Asia/Karachi",
        )
