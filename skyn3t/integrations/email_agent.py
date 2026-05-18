"""Email integration agent for SkyN3t."""

import asyncio
import email
import email.policy
import email.utils
import logging
import os
import re
import ssl
from typing import Any, Dict, List, Optional, Tuple

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger(__name__)


class EmailAgent(BaseAgent):
    """Agent that monitors an IMAP inbox, parses emails, routes to agents,
    and sends responses via SMTP.
    """

    def __init__(
        self,
        name: str = "email_agent",
        event_bus: Optional[EventBus] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="integration",
            provider="email",
            event_bus=event_bus or EventBus(),
            config=config or {},
        )
        self.add_capability(
            AgentCapability(
                name="email_monitoring",
                description="Monitor IMAP inbox and process incoming emails",
                parameters={"folder": "str", "criteria": "str"},
                required_config=["EMAIL_IMAP_HOST", "EMAIL_ADDRESS", "EMAIL_PASSWORD"],
            )
        )
        self.add_capability(
            AgentCapability(
                name="email_sending",
                description="Send emails via SMTP",
                parameters={"to": "str", "subject": "str", "body": "str", "html": "str"},
                required_config=["EMAIL_SMTP_HOST", "EMAIL_ADDRESS", "EMAIL_PASSWORD"],
            )
        )

        # IMAP config
        self.imap_host = self.config.get("imap_host") or os.getenv("EMAIL_IMAP_HOST")
        self.imap_port = int(str(self.config.get("imap_port") or os.getenv("EMAIL_IMAP_PORT", "993")))
        self.imap_ssl = self.config.get("imap_ssl", True)

        # SMTP config
        self.smtp_host = self.config.get("smtp_host") or os.getenv("EMAIL_SMTP_HOST")
        self.smtp_port = int(str(self.config.get("smtp_port") or os.getenv("EMAIL_SMTP_PORT", "587")))
        self.smtp_tls = self.config.get("smtp_tls", True)
        self.smtp_ssl = self.config.get("smtp_ssl", False)

        # Credentials
        self.email_address = self.config.get("email_address") or os.getenv("EMAIL_ADDRESS")
        self.email_password = self.config.get("email_password") or os.getenv("EMAIL_PASSWORD")

        # Behavior
        self.poll_interval = int(str(self.config.get("poll_interval", 60)))
        self.inbox_folder = self.config.get("inbox_folder", "INBOX")
        self.processed_folder = self.config.get("processed_folder", "SkyN3t_Processed")
        self.max_email_size = int(self.config.get("max_email_size", 5_000_000))
        self.auto_reply = self.config.get("auto_reply", True)
        self.allowed_senders = self.config.get("allowed_senders", [])
        self.blocked_senders = self.config.get("blocked_senders", [])
        self.subject_patterns = self.config.get(
            "subject_patterns",
            {
                r"\[SkyN3t\]|\[skyn3t\]": "direct",
                r"(?i)(bug|issue|error|crash|fail)": "issue_analysis",
                r"(?i)(review|pr|pull request|merge)": "pr_review",
                r"(?i)(code|function|script|refactor)": "code_execution",
                r"(?i)(question|help|how to|what is)": "chat",
            },
        )

        self._monitor_task: Optional[asyncio.Task] = None
        self._orchestrator: Any = None
        self._imap_client: Any = None

    async def initialize(self) -> None:
        """Validate configuration."""
        if not all([self.imap_host, self.smtp_host, self.email_address, self.email_password]):
            raise RuntimeError(
                "EmailAgent requires EMAIL_IMAP_HOST, EMAIL_SMTP_HOST, EMAIL_ADDRESS, EMAIL_PASSWORD"
            )
        if not self.smtp_ssl and not self.smtp_tls:
            raise RuntimeError(
                "EmailAgent refuses SMTP login without TLS or SSL. "
                "Enable smtp_tls or smtp_ssl before starting the agent."
            )

    async def health_check(self) -> bool:
        """Check email connectivity."""
        try:
            await self._check_imap_connection()
            return True
        except Exception:
            return False

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        """Execute an email-related task."""
        task_type = task.input_data.get("task_type", "send_email")

        if task_type == "send_email":
            return await self._send_email_task(task)
        elif task_type == "send_html_email":
            return await self._send_html_email_task(task)
        elif task_type == "fetch_unread":
            return await self._fetch_unread_task(task)
        elif task_type == "mark_read":
            return await self._mark_read_task(task)
        else:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"Unknown email task type: {task_type}",
            )

    async def start(self) -> None:
        """Start the email monitoring loop."""
        await super().start()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def shutdown(self) -> None:
        """Shutdown the email agent."""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        if self._imap_client:
            try:
                await self._run_sync(self._imap_client.logout)
            except Exception:
                pass
        await super().shutdown()

    def set_orchestrator(self, orchestrator: Any) -> None:
        """Attach the main orchestrator for task routing."""
        self._orchestrator = orchestrator

    def _get_imap_settings(self) -> tuple[str, int, str, str]:
        """Return validated IMAP connection settings."""
        if not isinstance(self.imap_host, str) or not self.imap_host:
            raise RuntimeError("EMAIL_IMAP_HOST is not configured")
        if not isinstance(self.email_address, str) or not self.email_address:
            raise RuntimeError("EMAIL_ADDRESS is not configured")
        if not isinstance(self.email_password, str) or not self.email_password:
            raise RuntimeError("EMAIL_PASSWORD is not configured")
        return self.imap_host, self.imap_port, self.email_address, self.email_password

    @staticmethod
    def _extract_email_bytes(fetch_data: Any) -> bytes | None:
        """Extract the raw RFC822 payload from an IMAP fetch result."""
        if not isinstance(fetch_data, list) or not fetch_data:
            return None
        first = fetch_data[0]
        if (
            isinstance(first, tuple)
            and len(first) > 1
            and isinstance(first[1], (bytes, bytearray))
        ):
            return bytes(first[1])
        return None

    # ------------------------------------------------------------------
    # IMAP monitoring loop
    # ------------------------------------------------------------------

    async def _monitor_loop(self):
        """Main loop that polls the inbox."""
        consecutive_failures = 0
        max_backoff = 300
        while self._running:
            try:
                await self._poll_inbox()
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                self._record_error(str(e), {"context": "monitor_loop", "failures": consecutive_failures})
                backoff = min(self.poll_interval * (2 ** (consecutive_failures - 1)), max_backoff)
                await asyncio.sleep(backoff)
                continue
            await asyncio.sleep(self.poll_interval)

    async def _poll_inbox(self):
        """Poll the inbox for new messages."""
        parsed_messages = await asyncio.to_thread(self._sync_poll_inbox)
        for parsed, msg_id_str in parsed_messages:
            try:
                await self._handle_parsed_message(parsed)
            except Exception as e:
                self._record_error(str(e), {"msg_id": msg_id_str})

    def _sync_poll_inbox(self) -> List[Tuple[Dict[str, Any], str]]:
        """Synchronously connect, fetch unread messages, mark as read, and disconnect."""
        import imaplib

        host, port, email_address, email_password = self._get_imap_settings()
        ctx = ssl.create_default_context()
        client = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
        client.login(email_address, email_password)
        client.select(self.inbox_folder)

        results: List[Tuple[Dict[str, Any], str]] = []
        try:
            status, data = client.search(None, "UNSEEN")
            if status != "OK":
                return results

            message_ids = data[0].split()
            for msg_id in message_ids:
                try:
                    fstatus, fdata = client.fetch(msg_id, "(RFC822)")
                    if fstatus != "OK":
                        continue
                    raw_email = self._extract_email_bytes(fdata)
                    if raw_email is None:
                        continue
                    msg = email.message_from_bytes(raw_email, policy=email.policy.default)
                    parsed = self._parse_email(msg)
                    client.store(msg_id, "+FLAGS", "\\Seen")
                    results.append((parsed, msg_id.decode()))
                except Exception as e:
                    self._record_error(str(e), {"msg_id": msg_id.decode()})
        finally:
            try:
                client.close()
            except Exception:
                pass
            try:
                client.logout()
            except Exception:
                pass
        return results

    async def _handle_parsed_message(self, parsed: Dict[str, Any]) -> None:
        """Apply filters and route a parsed message."""
        sender = parsed["from"]

        if self._is_blocked(sender):
            return
        if self.allowed_senders and not self._is_allowed(sender):
            return

        if self.auto_reply:
            asyncio.create_task(self._route_email_and_reply(parsed))

    async def _process_message(self, client, msg_id: bytes):
        """Process a single email message."""
        status, data = client.fetch(msg_id, "(RFC822)")
        if status != "OK":
            return

        raw_email = self._extract_email_bytes(data)
        if raw_email is None:
            return
        msg = email.message_from_bytes(raw_email, policy=email.policy.default)

        parsed = self._parse_email(msg)
        sender = parsed["from"]

        # Filtering
        if self._is_blocked(sender):
            return
        if self.allowed_senders and not self._is_allowed(sender):
            return

        # Route to agent
        if self.auto_reply:
            asyncio.create_task(self._route_email_and_reply(parsed))

        # Mark as read / move to processed
        client.store(msg_id, "+FLAGS", "\\Seen")

    def _parse_email(self, msg) -> Dict[str, Any]:
        """Parse an email.message.Message into a dict."""
        subject = msg.get("Subject", "")
        from_addr = msg.get("From", "")
        to_addr = msg.get("To", "")
        date_str = msg.get("Date", "")
        message_id = msg.get("Message-ID", "")

        body_text, body_html, attachments = self._extract_body(msg)

        return {
            "message_id": message_id,
            "subject": subject,
            "from": from_addr,
            "to": to_addr,
            "date": date_str,
            "body_text": body_text,
            "body_html": body_html,
            "attachments": attachments,
        }

    def _extract_body(self, msg) -> Tuple[str, str, List[Dict[str, Any]]]:
        """Extract text, html, and attachments from a message.

        Aggregate body size is capped at ``max_email_size`` across all parts
        combined; without this cap a malicious mail with many small parts
        could OOM the process even though no individual part trips the limit.
        """
        text_parts: List[str] = []
        html_parts: List[str] = []
        attachments: List[Dict[str, Any]] = []
        body_budget = int(self.max_email_size)
        consumed = 0

        def _take(decoded: str, into: List[str]) -> None:
            nonlocal consumed
            remaining = body_budget - consumed
            if remaining <= 0:
                return
            if len(decoded) > remaining:
                decoded = decoded[:remaining] + "\n... [truncated: aggregate body cap]"
            into.append(decoded)
            consumed += len(decoded)

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))
                filename = part.get_filename()

                if filename:
                    payload = part.get_payload(decode=True)
                    if payload and len(payload) <= self.max_email_size:
                        attachments.append({
                            "filename": filename,
                            "content_type": content_type,
                            "size": len(payload),
                        })
                elif content_type == "text/plain" and "attachment" not in content_disposition:
                    payload = part.get_payload(decode=True)
                    if payload:
                        _take(payload.decode("utf-8", errors="replace"), text_parts)
                elif content_type == "text/html" and "attachment" not in content_disposition:
                    payload = part.get_payload(decode=True)
                    if payload:
                        _take(payload.decode("utf-8", errors="replace"), html_parts)
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                decoded = payload.decode("utf-8", errors="replace")
                if msg.get_content_type() == "text/html":
                    _take(decoded, html_parts)
                else:
                    _take(decoded, text_parts)

        return "\n".join(text_parts), "\n".join(html_parts), attachments

    def _is_blocked(self, sender: str) -> bool:
        """Check if sender is blocked."""
        _, sender_addr = email.utils.parseaddr(sender or "")
        sender_addr = sender_addr.lower()
        for blocked in self.blocked_senders:
            _, blocked_addr = email.utils.parseaddr(blocked or "")
            if blocked_addr.lower() == sender_addr and sender_addr:
                return True
        return False

    def _is_allowed(self, sender: str) -> bool:
        """Check if sender is in the allowed list."""
        _, sender_addr = email.utils.parseaddr(sender or "")
        sender_addr = sender_addr.lower()
        for allowed in self.allowed_senders:
            _, allowed_addr = email.utils.parseaddr(allowed or "")
            if allowed_addr.lower() == sender_addr and sender_addr:
                return True
        return False

    def _classify_email(self, subject: str, body: str) -> Tuple[Optional[str], Dict[str, Any]]:
        """Classify an email and determine routing."""
        combined = f"{subject} {body}"
        for pattern, capability in self.subject_patterns.items():
            if re.search(pattern, combined):
                return capability, {"pattern": pattern, "matched": True}
        return None, {}

    # ------------------------------------------------------------------
    # Routing and response
    # ------------------------------------------------------------------

    async def _route_email_and_reply(self, parsed: Dict[str, Any]):
        """Route an email to agents and send a reply."""
        subject = parsed["subject"]
        body = parsed["body_text"] or parsed["body_html"]
        sender = parsed["from"]

        capability, match_info = self._classify_email(subject, body)

        if self._orchestrator is None:
            logger.warning("EmailAgent has no orchestrator attached; cannot route email.")
            return

        if self._orchestrator and capability:
            task = TaskRequest(
                title=f"Email: {subject[:80]}",
                description=body[:2000],
                input_data={
                    "prompt": body,
                    "subject": subject,
                    "sender": sender,
                    "task_type": capability,
                    "source": "email",
                },
                priority=1,
            )
            try:
                task_id = await self._orchestrator.submit_task(task, capability=capability)
                result = await self._orchestrator.wait_for_task(task_id, timeout=300.0)
                if result and result.success:
                    response_text = result.output.get(
                        "response",
                        result.output.get("stdout", str(result.output)),
                    )
                else:
                    response_text = f"I received your email but was unable to process it: {result.error if result else 'unknown error'}"
            except Exception as e:
                response_text = f"Error processing your request: {e}"
        else:
            response_text = (
                "Thank you for your email.\n\n"
                f"Subject: {subject}\n"
                "Your message has been logged and will be reviewed."
            )

        # Send reply
        reply_subject = f"Re: {subject}" if not subject.startswith("Re:") else subject
        await self._send_email(
            to=sender,
            subject=reply_subject,
            body=response_text,
            in_reply_to=parsed.get("message_id"),
        )

    # ------------------------------------------------------------------
    # SMTP helpers
    # ------------------------------------------------------------------

    async def _send_email(
        self,
        to: str,
        subject: str,
        body: str,
        html: Optional[str] = None,
        in_reply_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send an email via SMTP."""
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.email_address
        msg["To"] = to
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to

        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")

        def _send():
            if not self.smtp_ssl and not self.smtp_tls:
                raise RuntimeError(
                    "SMTP login requires TLS or SSL. "
                    "Enable smtp_tls or smtp_ssl before sending mail."
                )
            if self.smtp_ssl:
                server = smtplib.SMTP_SSL(
                    self.smtp_host,
                    self.smtp_port,
                    context=ssl.create_default_context(),
                )
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port)
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            server.login(self.email_address, self.email_password)
            server.send_message(msg)
            server.quit()

        await self._run_sync(_send)
        return {"sent": True, "to": to, "subject": subject}

    async def _check_imap_connection(self):
        """Verify IMAP connectivity."""
        def _sync_check():
            import imaplib

            ctx = ssl.create_default_context()
            client = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, ssl_context=ctx)
            client.login(self.email_address, self.email_password)
            client.select(self.inbox_folder)
            client.close()
            client.logout()

        await asyncio.to_thread(_sync_check)

    # ------------------------------------------------------------------
    # Task handlers
    # ------------------------------------------------------------------

    async def _send_email_task(self, task: TaskRequest) -> TaskResult:
        to = task.input_data.get("to")
        subject = task.input_data.get("subject", "")
        body = task.input_data.get("body", "")
        in_reply_to = task.input_data.get("in_reply_to")
        if not to:
            return TaskResult(task_id=task.task_id, success=False, error="Recipient 'to' is required")
        try:
            result = await self._send_email(to, subject, body, in_reply_to=in_reply_to)
            return TaskResult(task_id=task.task_id, success=True, output=result)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=str(e))

    async def _send_html_email_task(self, task: TaskRequest) -> TaskResult:
        to = task.input_data.get("to")
        subject = task.input_data.get("subject", "")
        body = task.input_data.get("body", "")
        html = task.input_data.get("html")
        if not to:
            return TaskResult(task_id=task.task_id, success=False, error="Recipient 'to' is required")
        try:
            result = await self._send_email(to, subject, body, html=html)
            return TaskResult(task_id=task.task_id, success=True, output=result)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=str(e))

    async def _fetch_unread_task(self, task: TaskRequest) -> TaskResult:
        folder = str(task.input_data.get("folder", self.inbox_folder))
        limit = task.input_data.get("limit", 10)

        def _sync_fetch_unread() -> List[Dict[str, Any]]:
            import imaplib
            host, port, email_address, email_password = self._get_imap_settings()
            ctx = ssl.create_default_context()
            client = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
            client.login(email_address, email_password)
            client.select(folder)
            try:
                status, data = client.search(None, "UNSEEN")
                messages: List[Dict[str, Any]] = []
                if status == "OK":
                    msg_ids = data[0].split()[-limit:]
                    for msg_id in msg_ids:
                        _, d = client.fetch(msg_id, "(RFC822)")
                        raw = self._extract_email_bytes(d)
                        if raw is None:
                            continue
                        msg = email.message_from_bytes(raw, policy=email.policy.default)
                        parsed = self._parse_email(msg)
                        messages.append(parsed)
                return messages
            finally:
                try:
                    client.close()
                except Exception:
                    pass
                try:
                    client.logout()
                except Exception:
                    pass

        try:
            messages = await asyncio.to_thread(_sync_fetch_unread)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output={"messages": messages, "count": len(messages)},
            )
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=str(e))

    async def _mark_read_task(self, task: TaskRequest) -> TaskResult:
        folder = str(task.input_data.get("folder", self.inbox_folder))
        msg_ids = task.input_data.get("msg_ids", [])
        if not msg_ids:
            return TaskResult(task_id=task.task_id, success=False, error="msg_ids required")

        def _sync_mark_read() -> int:
            import imaplib
            host, port, email_address, email_password = self._get_imap_settings()
            ctx = ssl.create_default_context()
            client = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
            client.login(email_address, email_password)
            client.select(folder)
            try:
                for m in msg_ids:
                    client.store(m, "+FLAGS", "\\Seen")
                return len(msg_ids)
            finally:
                try:
                    client.close()
                except Exception:
                    pass
                try:
                    client.logout()
                except Exception:
                    pass

        try:
            count = await asyncio.to_thread(_sync_mark_read)
            return TaskResult(task_id=task.task_id, success=True, output={"marked": count})
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=str(e))

    @staticmethod
    async def _run_sync(fn, *args, **kwargs):
        """Run a synchronous function in a thread pool.

        Uses asyncio.to_thread which is the supported replacement for
        ``get_event_loop().run_in_executor`` (the latter is deprecated and
        raises in 3.12+ when called from an async context with no running loop).
        """
        return await asyncio.to_thread(fn, *args, **kwargs)
