from __future__ import annotations

from email.utils import parseaddr

from .config import Config

from .graph import MAIL_SCOPES, get_graph_client


def _build_sender(config: Config):
    from msgraph.generated.models.recipient import Recipient
    from msgraph.generated.models.email_address import EmailAddress

    sender = (config.email.sender or "").strip()
    if not sender:
        return None

    sender_name = (getattr(config.email, "sender_name", "") or "").strip()
    parsed_name, parsed_address = parseaddr(sender)
    if not parsed_address:
        parsed_address = sender

    if not parsed_address:
        return None

    resolved_name = sender_name or (parsed_name.strip() if parsed_name else None)
    return Recipient(
        email_address=EmailAddress(
            address=parsed_address,
            name=resolved_name or None,
        )
    )


async def send_email_with_links(
    subject: str,
    body: str,
    recipients: list[str],
    config: Config,
    logger,
) -> bool:
    client = get_graph_client(config, logger, MAIL_SCOPES)
    if not client:
        return False

    try:
        from msgraph.generated.models.message import Message
        from msgraph.generated.models.item_body import ItemBody
        from msgraph.generated.models.body_type import BodyType
        from msgraph.generated.models.recipient import Recipient
        from msgraph.generated.models.email_address import EmailAddress
        from msgraph.generated.users.item.send_mail.send_mail_post_request_body import (
            SendMailPostRequestBody,
        )

        message_kwargs = dict(
            subject=subject,
            body=ItemBody(content_type=BodyType.Html, content=body),
            to_recipients=[
                Recipient(email_address=EmailAddress(address=r)) for r in recipients
            ],
        )

        sender = _build_sender(config)
        if sender:
            message_kwargs["from_"] = sender
            message_kwargs["sender"] = sender

        message = Message(**message_kwargs)

        request_body = SendMailPostRequestBody(message=message, save_to_sent_items=True)
        await client.me.send_mail.post(request_body)
        logger.info(f"Email sent to {recipients}")
        return True

    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


async def send_error_email(
    error_message: str,
    config: Config,
    logger,
) -> None:
    if not config.is_production:
        logger.debug("Not in production mode, skipping error email")
        return

    if not config.user.email:
        logger.warning("User email not configured, cannot send error email")
        return

    if not config.email.sender or not config.email.recipients:
        logger.warning("Email not configured, cannot send error email")
        return

    body = f"""
    <h2>Anthropic Readings Daemon Error</h2>
    <p>The daemon encountered an error:</p>
    <pre>{error_message}</pre>
    <p>Please check the logs for more details.</p>
    """

    subject = "Anthropic Readings Daemon Error"
    await send_email_with_links(subject, body, config.email.recipients, config, logger)
