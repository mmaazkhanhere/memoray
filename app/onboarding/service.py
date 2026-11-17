import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.contracts import IncomingContact, IncomingMessage, OutgoingMessage
from app.models import (
    ChannelAccount,
    ChannelAccountState,
    ChannelIdentity,
    ChannelIdentityStatus,
    ChannelKind,
    User,
    UserStatus,
)
from app.phones.service import PhoneService
from app.time_utils import validate_timezone
from app.mcp.auth.service import McpAuthService, OAuthError


@dataclass(frozen=True)
class OnboardingResult:
    account: ChannelAccount
    replies: list[OutgoingMessage]
    handled: bool


class OnboardingService:
    def __init__(self, db: Session, calendar_service=None):
        self.db = db
        self.phones = PhoneService(db)
        self.calendar_service = calendar_service

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

    def handle_completed(self, account: ChannelAccount, message: IncomingMessage) -> OnboardingResult:
        """Handle phone linking requested after onboarding has already completed."""
        command = (message.text or "").strip().casefold()
        if command.startswith("/start mcp_"):
            nonce = (message.text or "").strip().split(None, 1)[1].removeprefix("mcp_").strip()
            return self._connect_chatgpt(account, nonce)
        if command.startswith("/connect_chatgpt ") or command.startswith("/connect chatgpt "):
            nonce = (message.text or "").split(None, 1)[1].strip()
            return self._connect_chatgpt(account, nonce)
        if command in {"/connect_calendar", "/connect calendar"}:
            return self._calendar_connect(account)
        if message.contact is not None:
            return self._phone_decision(account, message)
        if command not in {"/link phone", "/link_phone", "/add phone", "/phone"}:
            return OnboardingResult(account, [], False)
        account.onboarding_state = ChannelAccountState.needs_phone_decision
        self.db.flush()
        return OnboardingResult(
            account,
            [
                self._reply(
                    account,
                    "Share your Telegram contact to link your phone for live calls, or send /cancel.",
                    request_phone=True,
                )
            ],
            True,
        )

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
            pending_mcp_nonce=self._mcp_nonce_from_message(message),
        )
        self.db.add(account)
        self.db.flush()
        return account

    def _start(self, account: ChannelAccount, message: IncomingMessage) -> OnboardingResult:
        command = (message.text or "").strip()
        if command.casefold().startswith("/start mcp_"):
            nonce = command.split(None, 1)[1].removeprefix("mcp_").strip()
            if account.onboarding_state == ChannelAccountState.completed:
                return self._connect_chatgpt(account, nonce)
            account.pending_mcp_nonce = nonce
            self.db.flush()
        if account.onboarding_state == ChannelAccountState.completed:
            return OnboardingResult(
                account, [self._reply(account, "Welcome back! How can I help?")], True
            )
        return OnboardingResult(account, [self._prompt(account)], True)

    def _connect_chatgpt(self, account: ChannelAccount, nonce: str) -> OnboardingResult:
        try:
            McpAuthService(self.db).approve_nonce(nonce, account.user.id)
            self.db.flush()
        except OAuthError as exc:
            return OnboardingResult(
                account,
                [self._reply(account, f"I couldn't connect ChatGPT: {exc.description}")],
                True,
            )
        return OnboardingResult(
            account,
            [
                self._reply(
                    account,
                    "ChatGPT is now connected to this Memoray account. You can close Telegram and continue there.",
                )
            ],
            True,
        )

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
        link_reply = self._approve_pending_mcp_link(account)
        self.db.flush()
        replies = [self._reply(account, "Would you like to link your phone for live calls? Share your contact or tap Skip for now.", request_phone=True)]
        if link_reply:
            replies.append(link_reply)
        return OnboardingResult(account, replies, True)

    def _phone_decision(
        self, account: ChannelAccount, message: IncomingMessage
    ) -> OnboardingResult:
        if (message.text or "").strip().casefold() in {"/connect_calendar", "/connect calendar"}:
            return self._calendar_connect(account)
        if message.contact is not None:
            try:
                number = self.phones.normalize_contact(
                    sender_id=message.external_user_id,
                    owner_external_id=message.contact.owner_external_id,
                    raw_number=message.contact.phone_number,
                )
            except ValueError as exc:
                return OnboardingResult(
                    account,
                    [self._reply(account, str(exc), request_phone=True)],
                    True,
                )
            return self._link_phone_number(account, number)

        if (message.text or "").strip().casefold() in {"skip", "skip for now", "no", "not now"}:
            account.onboarding_state = ChannelAccountState.completed
            self._approve_pending_mcp_link(account)
            self.db.flush()
            return OnboardingResult(
                account,
                [
                    self._reply(
                        account,
                        "No problem. You can use the agent here; live calls need a linked phone. "
                        "Google Calendar is optional too—send /connect_calendar any time.",
                    )
                ],
                True,
            )
        if (message.text or "").strip().casefold() in {"/cancel", "cancel"}:
            account.onboarding_state = ChannelAccountState.completed
            self._approve_pending_mcp_link(account)
            self.db.flush()
            return OnboardingResult(
                account,
                [
                    self._reply(
                        account,
                        "No problem. Phone linking remains optional. "
                        "Google Calendar is optional too—send /connect_calendar any time.",
                    )
                ],
                True,
            )
        candidate = (message.text or "").strip()
        if candidate.startswith("+") or candidate.startswith("00"):
            try:
                number = self.phones.normalize_e164(candidate)
            except ValueError:
                return OnboardingResult(
                    account,
                    [
                        self._reply(
                            account,
                            "That phone number is invalid. Use an international number such as +14155550123.",
                            request_phone=True,
                        )
                    ],
                    True,
                )
            return self._link_phone_number(account, number)
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

    def _link_phone_number(self, account: ChannelAccount, number: str) -> OnboardingResult:
        try:
            self.phones.link(account.user.id, number)
            has_default_phone = self.db.scalar(
                select(ChannelIdentity.id).where(
                    ChannelIdentity.user_id == account.user.id,
                    ChannelIdentity.channel == ChannelKind.phone,
                    ChannelIdentity.status == ChannelIdentityStatus.linked,
                    ChannelIdentity.is_default.is_(True),
                )
            )
            self.db.add(
                ChannelIdentity(
                    user_id=account.user.id,
                    channel=ChannelKind.phone,
                    external_principal_id=number,
                    external_destination_id=number,
                    status=ChannelIdentityStatus.linked,
                    is_default=has_default_phone is None,
                    verified_at=datetime.now(timezone.utc),
                )
            )
        except ValueError as exc:
            return OnboardingResult(
                account,
                [self._reply(account, str(exc), request_phone=True)],
                True,
            )
        account.onboarding_state = ChannelAccountState.completed
        self._approve_pending_mcp_link(account)
        self.db.flush()
        return OnboardingResult(
            account,
            [
                self._reply(
                    account,
                    "Your phone is linked. You can now use the agent here and on live calls. "
                    "Google Calendar is optional—send /connect_calendar any time.",
                )
            ],
            True,
        )

    @staticmethod
    def _mcp_nonce_from_message(message: IncomingMessage) -> str | None:
        command = (message.text or "").strip()
        if command.casefold().startswith("/start mcp_"):
            return command.split(None, 1)[1].removeprefix("mcp_").strip() or None
        return None

    def _approve_pending_mcp_link(self, account: ChannelAccount) -> None:
        nonce = account.pending_mcp_nonce
        if not nonce:
            return
        account.pending_mcp_nonce = None
        try:
            McpAuthService(self.db).approve_nonce(nonce, account.user.id)
        except OAuthError:
            # The browser will display the authorization failure; onboarding must remain usable.
            return

    def _calendar_connect(self, account: ChannelAccount) -> OnboardingResult:
        service = self.calendar_service
        if service is None:
            from app.integrations.google_calendar.service import GoogleCalendarService

            service = GoogleCalendarService(self.db)
        return OnboardingResult(
            account,
            [self._reply(account, service.connect_command(account.user.id))],
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
