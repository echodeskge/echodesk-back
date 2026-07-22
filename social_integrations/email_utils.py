"""
Email utility functions for IMAP/SMTP operations.
"""
import imaplib
import smtplib
import ssl
import socket
import email
import hashlib
import logging
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import make_msgid, formataddr, parseaddr, getaddresses, parsedate_to_datetime
from email.header import decode_header as email_decode_header
from datetime import datetime, timedelta
from typing import Tuple, Optional, List, Dict, Any

from django.utils import timezone
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)


def decode_imap_utf7(s: str) -> str:
    """
    Decode IMAP Modified UTF-7 encoded folder names.

    IMAP uses a modified version of UTF-7 for folder names containing non-ASCII characters.
    Example: "&ENAQ4A- &EOwQ0BDoENAQ2hDdENc-" decodes to "არ წაშალოთ" (Georgian)

    Args:
        s: The IMAP Modified UTF-7 encoded string

    Returns:
        The decoded Unicode string
    """
    if s is None:
        return ''
    if not s or '&' not in s:
        return s

    result = []
    i = 0
    while i < len(s):
        if s[i] == '&':
            end = s.find('-', i)
            if end == -1:
                result.append(s[i:])
                break
            if end == i + 1:
                # "&-" represents a literal "&"
                result.append('&')
            else:
                encoded = s[i+1:end]
                # Replace , with / for standard base64
                encoded = encoded.replace(',', '/')
                # Add padding if needed
                padding = (4 - len(encoded) % 4) % 4
                encoded += '=' * padding
                try:
                    decoded = base64.b64decode(encoded).decode('utf-16-be')
                    result.append(decoded)
                except Exception:
                    # If decoding fails, keep original
                    result.append(s[i:end+1])
            i = end + 1
        else:
            result.append(s[i])
            i += 1
    return ''.join(result)


def decode_mime_header(header_value: str) -> str:
    """
    Decode MIME-encoded email headers (RFC 2047).

    Handles encoded headers like:
    - =?UTF-8?B?6IyX6KGM?= (Base64 encoded UTF-8)
    - =?UTF-8?Q?Hello_World?= (Quoted-Printable encoded)

    Args:
        header_value: The potentially encoded header value

    Returns:
        The decoded string
    """
    if not header_value:
        return ''

    try:
        decoded_parts = email_decode_header(header_value)
        result_parts = []

        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                # Decode bytes using specified charset or default to utf-8
                encoding = charset or 'utf-8'
                try:
                    result_parts.append(part.decode(encoding, errors='replace'))
                except (LookupError, UnicodeDecodeError):
                    # If charset is unknown, try utf-8
                    result_parts.append(part.decode('utf-8', errors='replace'))
            else:
                result_parts.append(part)

        return ''.join(result_parts)
    except Exception as e:
        logger.warning(f"Failed to decode MIME header: {e}")
        return header_value


def encode_imap_utf7(s: str) -> str:
    """
    Encode a Unicode string to IMAP Modified UTF-7.

    This is the reverse of decode_imap_utf7(). Used when we need to
    communicate folder names back to IMAP server.

    Example: "არ წაშალოთ" encodes to "&ENAQ4A- &EOwQ0BDoENAQ2hDdENc-"

    Args:
        s: The Unicode string to encode

    Returns:
        The IMAP Modified UTF-7 encoded string
    """
    if not s:
        return s

    # Check if string is pure ASCII (no encoding needed)
    try:
        s.encode('ascii')
        # String is pure ASCII, just escape & as &-
        return s.replace('&', '&-')
    except UnicodeEncodeError:
        pass

    result = []
    i = 0
    while i < len(s):
        c = s[i]
        # Check if character is ASCII printable (except &)
        if c == '&':
            result.append('&-')
            i += 1
        elif ord(c) >= 0x20 and ord(c) <= 0x7e:
            result.append(c)
            i += 1
        else:
            # Start of non-ASCII sequence - collect all non-ASCII chars
            non_ascii_start = i
            while i < len(s):
                c = s[i]
                if c == '&' or (ord(c) >= 0x20 and ord(c) <= 0x7e):
                    break
                i += 1

            # Encode the non-ASCII sequence
            non_ascii_str = s[non_ascii_start:i]
            utf16_bytes = non_ascii_str.encode('utf-16-be')
            b64_encoded = base64.b64encode(utf16_bytes).decode('ascii')
            # Remove padding and replace / with ,
            b64_encoded = b64_encoded.rstrip('=').replace('/', ',')
            result.append('&' + b64_encoded + '-')

    return ''.join(result)


def _imap_authenticate(imap, username: str, password: str) -> None:
    """
    Authenticate an IMAP connection, falling back to AUTHENTICATE PLAIN when LOGIN fails.

    Some IMAP servers (e.g. those using Dovecot) reject the LOGIN command with
    "BAD LOGIN parse error" when the password contains special characters such as
    spaces, parentheses, backslashes or double-quotes.  The IMAP AUTHENTICATE PLAIN
    mechanism base64-encodes the credentials and therefore avoids all quoting issues.

    We try LOGIN first (most compatible) and fall back to AUTHENTICATE PLAIN only
    when we get the specific "LOGIN parse error" / "BAD" response, to avoid masking
    genuine authentication failures (wrong password etc.).

    Args:
        imap: An IMAP4 or IMAP4_SSL instance that is already connected.
        username: IMAP username.
        password: Plain-text password.

    Raises:
        imaplib.IMAP4.error: on authentication failure (wrong credentials etc.)
    """
    try:
        imap.login(username, password)
    except imaplib.IMAP4.error as e:
        err_str = str(e).lower()
        # Only fall back for parse / literal errors, not wrong-password errors
        if 'parse error' in err_str or ('bad' in err_str and 'login' in err_str):
            logger.warning(
                "IMAP LOGIN command failed with parse error (likely special chars in password); "
                "retrying with AUTHENTICATE PLAIN: %s", e
            )
            # Build SASL PLAIN token: \x00username\x00password
            # imaplib.authenticate() base64-encodes the callback return value itself,
            # so we must return raw bytes, NOT pre-encoded.
            plain_token = f'\x00{username}\x00{password}'.encode('utf-8')
            imap.authenticate('PLAIN', lambda _: plain_token)
        else:
            raise


def test_imap_connection(server: str, port: int, username: str, password: str, use_ssl: bool = True) -> Tuple[bool, Optional[str]]:
    """
    Test IMAP connection credentials.

    Returns:
        Tuple of (success: bool, error_message: Optional[str])
    """
    try:
        if use_ssl:
            imap = imaplib.IMAP4_SSL(server, port, timeout=10)
        else:
            imap = imaplib.IMAP4(server, port)
            imap.starttls()

        _imap_authenticate(imap, username, password)
        imap.logout()
        return True, None
    except imaplib.IMAP4.error as e:
        return False, f"IMAP authentication error: {str(e)}"
    except Exception as e:
        return False, f"IMAP connection error: {str(e)}"


def test_smtp_connection(server: str, port: int, username: str, password: str,
                         use_tls: bool = True, use_ssl: bool = False) -> Tuple[bool, Optional[str]]:
    """
    Test SMTP connection credentials.

    Returns:
        Tuple of (success: bool, error_message: Optional[str])
    """
    try:
        if use_ssl:
            smtp = smtplib.SMTP_SSL(server, port, timeout=10)
        else:
            smtp = smtplib.SMTP(server, port, timeout=10)
            if use_tls:
                smtp.starttls()

        smtp.login(username, password)
        smtp.quit()
        return True, None
    except smtplib.SMTPAuthenticationError as e:
        return False, f"SMTP authentication error: {str(e)}"
    except smtplib.SMTPException as e:
        return False, f"SMTP error: {str(e)}"
    except Exception as e:
        return False, f"SMTP connection error: {str(e)}"


def get_imap_folders(connection) -> Dict:
    """
    Get list of available folders from IMAP server.

    Args:
        connection: EmailConnection model instance

    Returns:
        Dict with 'success', 'folders', and optionally 'error'
    """
    try:
        if connection.imap_use_ssl:
            imap = imaplib.IMAP4_SSL(connection.imap_server, connection.imap_port, timeout=10)
        else:
            imap = imaplib.IMAP4(connection.imap_server, connection.imap_port)
            imap.starttls()

        _imap_authenticate(imap, connection.username, connection.get_password())
        result, folder_list = imap.list()
        imap.logout()

        folders = []
        if result == 'OK':
            for folder_data in folder_list:
                if isinstance(folder_data, bytes):
                    # Parse folder name from IMAP response like: (\HasNoChildren) "/" "INBOX"
                    decoded = folder_data.decode('utf-8', errors='replace')
                    # Extract the folder name (last quoted string or last word)
                    parts = decoded.rsplit('" ', 1)
                    if len(parts) == 2:
                        folder_name = parts[1].strip('"')
                    else:
                        # Fallback: get last part after delimiter
                        parts = decoded.split(' ')
                        folder_name = parts[-1].strip('"')
                    folders.append(decode_imap_utf7(folder_name))

        return {'success': True, 'folders': folders}
    except Exception as e:
        logger.error(f"Failed to get IMAP folders: {e}")
        return {'success': False, 'folders': [], 'error': str(e)}


def parse_address_list(address_string: str) -> List[Dict[str, str]]:
    """
    Parse email address list into [{email, name}] format.

    Args:
        address_string: Email address string (e.g., "John Doe <john@example.com>, jane@example.com")

    Returns:
        List of dicts with 'email' and 'name' keys
    """
    addresses = getaddresses([address_string])
    return [{'email': addr[1], 'name': decode_mime_header(addr[0])} for addr in addresses if addr[1]]


def extract_body(email_message) -> Tuple[str, str]:
    """
    Extract plain text and HTML body from email message.

    Args:
        email_message: email.message.Message object

    Returns:
        Tuple of (body_text, body_html)
    """
    body_text = ''
    body_html = ''

    if email_message.is_multipart():
        for part in email_message.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get('Content-Disposition', ''))

            # Skip attachments
            if 'attachment' in content_disposition:
                continue

            try:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or 'utf-8'
                    decoded = payload.decode(charset, errors='replace')

                    if content_type == 'text/plain' and not body_text:
                        body_text = decoded
                    elif content_type == 'text/html' and not body_html:
                        body_html = decoded
            except Exception as e:
                logger.warning(f"Failed to decode email part: {e}")
                continue
    else:
        content_type = email_message.get_content_type()
        try:
            payload = email_message.get_payload(decode=True)
            if payload:
                charset = email_message.get_content_charset() or 'utf-8'
                decoded = payload.decode(charset, errors='replace')

                if content_type == 'text/html':
                    body_html = decoded
                else:
                    body_text = decoded
        except Exception as e:
            logger.warning(f"Failed to decode email body: {e}")

    return body_text, body_html


def extract_attachments(email_message, connection) -> List[Dict[str, Any]]:
    """
    Extract and save attachments from email message.

    Args:
        email_message: email.message.Message object
        connection: EmailConnection model instance

    Returns:
        List of attachment dicts with filename, content_type, url, size
    """
    attachments = []

    if not email_message.is_multipart():
        return attachments

    for part in email_message.walk():
        content_disposition = str(part.get('Content-Disposition', ''))
        if 'attachment' not in content_disposition and 'inline' not in content_disposition:
            continue

        # Skip text parts that are part of the body
        if part.get_content_maintype() == 'text' and 'attachment' not in content_disposition:
            continue

        filename = part.get_filename()
        if not filename:
            # Generate filename for inline content
            ext = part.get_content_type().split('/')[-1]
            filename = f"attachment.{ext}"

        content_type = part.get_content_type()

        try:
            payload = part.get_payload(decode=True)
            if payload:
                # Save to storage
                file_path = f'email_attachments/{connection.id}/{timezone.now().strftime("%Y/%m/%d")}/{filename}'
                saved_path = default_storage.save(file_path, ContentFile(payload))
                url = default_storage.url(saved_path)

                attachments.append({
                    'filename': filename,
                    'content_type': content_type,
                    'url': url,
                    'size': len(payload)
                })
        except Exception as e:
            logger.error(f"Failed to save attachment {filename}: {e}")
            continue

    return attachments


def wrap_html_email(body_html: str, signature_html: str = None) -> str:
    """
    Wrap HTML content in a proper email template with inline styles.

    This ensures consistent rendering across email clients like Outlook, Gmail, etc.

    Args:
        body_html: The main email body HTML
        signature_html: Optional signature HTML to append

    Returns:
        Complete HTML email with inline styles
    """
    import re

    def process_images(html):
        """Add explicit width/height styles to images based on their attributes."""
        if not html:
            return html

        def fix_img(match):
            tag = match.group(0)
            attrs = match.group(1)

            # Extract width and height attributes
            width_match = re.search(r'width=["\']?(\d+)["\']?', attrs, re.IGNORECASE)
            height_match = re.search(r'height=["\']?(\d+)["\']?', attrs, re.IGNORECASE)

            if not width_match and not height_match:
                return tag

            width = width_match.group(1) if width_match else None
            height = height_match.group(1) if height_match else None

            # Build style string
            styles = []
            if width:
                styles.append(f'width: {width}px')
            if height:
                styles.append(f'height: {height}px')
            styles.append('max-width: none')  # Override any max-width: 100%

            style_str = '; '.join(styles)

            # Remove existing max-width from style if present
            tag = re.sub(r'max-width:\s*[^;]+;?\s*', '', tag, flags=re.IGNORECASE)

            # Add or update style attribute
            if 'style=' in tag.lower():
                # Append to existing style
                tag = re.sub(
                    r'style=(["\'])([^"\']*)\1',
                    lambda m: f'style="{m.group(2)}; {style_str}"',
                    tag,
                    count=1,
                    flags=re.IGNORECASE
                )
            else:
                # Add new style attribute
                tag = tag.replace('<img ', f'<img style="{style_str}" ', 1)

            return tag

        return re.sub(r'<img\s+([^>]*)>', fix_img, html, flags=re.IGNORECASE)

    # Add default link styling to signature HTML if links don't have inline styles
    styled_signature = signature_html
    if styled_signature:
        # Process images first
        styled_signature = process_images(styled_signature)

        # Find <a> tags without style attribute and add blue color
        def add_link_style(match):
            tag = match.group(0)
            if 'style=' in tag.lower():
                # Already has style, ensure color is set
                if 'color:' not in tag.lower() and 'color :' not in tag.lower():
                    # Add color to existing style
                    tag = re.sub(r'style="', 'style="color: #0066cc; ', tag, flags=re.IGNORECASE)
                    tag = re.sub(r"style='", "style='color: #0066cc; ", tag, flags=re.IGNORECASE)
                return tag
            else:
                # No style attribute, add one
                return tag.replace('<a ', '<a style="color: #0066cc; text-decoration: underline;" ', 1)
        styled_signature = re.sub(r'<a\s[^>]*>', add_link_style, styled_signature, flags=re.IGNORECASE)

    # Build the signature section with proper separator
    signature_section = ""
    if styled_signature:
        signature_section = f'''
        <tr>
            <td style="padding-top: 20px; border-top: 1px solid #e5e5e5; margin-top: 20px;">
                <div style="color: #666666; font-size: 14px; line-height: 1.5;">
                    {styled_signature}
                </div>
            </td>
        </tr>
        '''

    # Wrap in email-safe HTML template
    html_template = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="X-UA-Compatible" content="IE=edge">
    <title>Email</title>
    <!--[if mso]>
    <style type="text/css">
        body, table, td {{font-family: Arial, Helvetica, sans-serif !important;}}
    </style>
    <![endif]-->
    <style type="text/css">
        a {{color: #0066cc; text-decoration: underline;}}
    </style>
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #333333; background-color: #ffffff;">
    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="max-width: 600px;">
        <tr>
            <td style="padding: 0;">
                <div style="font-size: 14px; line-height: 1.6; color: #333333;">
                    {body_html}
                </div>
            </td>
        </tr>
        {signature_section}
    </table>
</body>
</html>'''

    return html_template


def compute_thread_id(message_id: str, in_reply_to: str, references: str) -> str:
    """
    Compute a thread ID for grouping related emails.

    Uses the first reference or in_reply_to as thread root.
    For new threads, uses the message ID.

    Args:
        message_id: The Message-ID header
        in_reply_to: The In-Reply-To header
        references: The References header

    Returns:
        A consistent thread ID string
    """
    # Use the first reference (root of thread) as thread ID
    if references:
        first_ref = references.split()[0].strip('<>').strip()
        return hashlib.md5(first_ref.encode()).hexdigest()[:32]
    elif in_reply_to:
        return hashlib.md5(in_reply_to.strip('<>').strip().encode()).hexdigest()[:32]
    else:
        # New thread - use message ID
        return hashlib.md5(message_id.strip('<>').strip().encode()).hexdigest()[:32]


def send_email_smtp(connection, to_emails: List[str], cc_emails: List[str] = None,
                    bcc_emails: List[str] = None, subject: str = '',
                    body_text: str = '', body_html: str = '',
                    reply_to_message_id: str = None, attachments: List[Dict] = None) -> Tuple[str, Any]:
    """
    Send an email via SMTP.

    Args:
        connection: EmailConnection model instance
        to_emails: List of recipient email addresses
        cc_emails: List of CC email addresses
        bcc_emails: List of BCC email addresses
        subject: Email subject
        body_text: Plain text body
        body_html: HTML body
        reply_to_message_id: Message-ID of the email being replied to (for threading)
        attachments: List of attachment dicts (not implemented yet)

    Returns:
        Tuple of (message_id, saved_EmailMessage)
    """
    from .models import EmailMessage, EmailSignature

    cc_emails = cc_emails or []
    bcc_emails = bcc_emails or []
    attachments = attachments or []

    # Check if we should append email signature
    # Priority: 1. Connection-specific signature, 2. Global EmailSignature
    is_reply = reply_to_message_id is not None
    signature_html_content = None
    signature_text_content = None

    try:
        # First, check if the connection has its own signature explicitly enabled
        if connection.signature_enabled and (connection.signature_html or connection.signature_text):
            # Use connection-specific signature
            if connection.signature_html:
                signature_html_content = connection.signature_html
            elif connection.signature_text:
                signature_html_content = connection.signature_text.replace('\n', '<br>')

            if connection.signature_text:
                signature_text_content = connection.signature_text
            elif connection.signature_html:
                import re
                sig_text = re.sub(r'<[^>]+>', '', connection.signature_html)
                sig_text = sig_text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&').strip()
                signature_text_content = sig_text
        else:
            # Fall back to global EmailSignature
            signature = EmailSignature.objects.first()
            if signature and signature.is_enabled:
                # Check if signature should be included (always for new emails, conditional for replies)
                should_include = not is_reply or signature.include_on_reply
                if should_include:
                    # Get signature content
                    if signature.signature_html:
                        signature_html_content = signature.signature_html
                    elif signature.signature_text:
                        # Convert plain text signature to HTML
                        signature_html_content = signature.signature_text.replace('\n', '<br>')

                    # Get plain text signature
                    if signature.signature_text:
                        signature_text_content = signature.signature_text
                    elif signature.signature_html:
                        # Simple HTML to text fallback - strip tags
                        import re
                        sig_text = re.sub(r'<[^>]+>', '', signature.signature_html)
                        sig_text = sig_text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&').strip()
                        signature_text_content = sig_text
    except Exception as e:
        logger.warning(f"Failed to get email signature: {e}")

    # Append signature to plain text body
    final_body_text = body_text
    if signature_text_content and body_text:
        final_body_text = f"{body_text}\n\n--\n{signature_text_content}"

    # Wrap HTML body in proper email template with signature
    final_body_html = None
    if body_html:
        final_body_html = wrap_html_email(body_html, signature_html_content)

    # Create message body
    if final_body_html and final_body_text:
        body_part = MIMEMultipart('alternative')
        body_part.attach(MIMEText(final_body_text, 'plain', 'utf-8'))
        body_part.attach(MIMEText(final_body_html, 'html', 'utf-8'))
    elif final_body_html:
        body_part = MIMEText(final_body_html, 'html', 'utf-8')
    else:
        body_part = MIMEText(final_body_text or '', 'plain', 'utf-8')

    # Wrap in 'mixed' if there are attachments, otherwise use body directly
    attachment_metadata = []
    if attachments:
        msg = MIMEMultipart('mixed')
        msg.attach(body_part)
        for att in attachments:
            maintype, _, subtype = (att.get('content_type') or 'application/octet-stream').partition('/')
            if not subtype:
                subtype = 'octet-stream'
            mime_att = MIMEBase(maintype, subtype)
            mime_att.set_payload(att['content'])
            encoders.encode_base64(mime_att)
            mime_att.add_header('Content-Disposition', 'attachment', filename=att.get('filename', 'file'))
            msg.attach(mime_att)
            attachment_metadata.append({
                'filename': att.get('filename', 'file'),
                'content_type': att.get('content_type', 'application/octet-stream'),
                'size': len(att.get('content', b'')),
            })
    else:
        msg = body_part

    # Set headers
    msg['Message-ID'] = make_msgid()
    msg['From'] = formataddr((connection.display_name or '', connection.email_address))
    msg['To'] = ', '.join(to_emails)
    if cc_emails:
        msg['Cc'] = ', '.join(cc_emails)
    msg['Subject'] = subject
    msg['Date'] = timezone.now().strftime('%a, %d %b %Y %H:%M:%S %z')

    # Handle reply threading
    thread_id = None
    original = None
    if reply_to_message_id:
        original = EmailMessage.objects.filter(message_id=reply_to_message_id).first()
        if original:
            msg['In-Reply-To'] = original.message_id
            refs = f"{original.references} {original.message_id}".strip()
            msg['References'] = refs
            thread_id = original.thread_id

    if not thread_id:
        thread_id = compute_thread_id(msg['Message-ID'], '', '')

    # Send via SMTP
    try:
        if connection.smtp_use_ssl:
            smtp = smtplib.SMTP_SSL(connection.smtp_server, connection.smtp_port, timeout=30)
        else:
            smtp = smtplib.SMTP(connection.smtp_server, connection.smtp_port, timeout=30)
            if connection.smtp_use_tls:
                smtp.starttls()

        smtp.login(connection.username, connection.get_password())

        all_recipients = to_emails + cc_emails + bcc_emails
        smtp.sendmail(connection.email_address, all_recipients, msg.as_string())
        smtp.quit()

    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        raise

    # Save sent message to database
    sent_message = EmailMessage.objects.create(
        connection=connection,
        message_id=msg['Message-ID'],
        thread_id=thread_id,
        in_reply_to=msg.get('In-Reply-To', ''),
        references=msg.get('References', ''),
        from_email=connection.email_address,
        from_name=connection.display_name or '',
        to_emails=[{'email': e, 'name': ''} for e in to_emails],
        cc_emails=[{'email': e, 'name': ''} for e in cc_emails],
        bcc_emails=[{'email': e, 'name': ''} for e in bcc_emails],
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        timestamp=timezone.now(),
        folder='Sent',
        is_from_business=True,
        is_read=True,
        attachments=attachment_metadata,
    )

    return msg['Message-ID'], sent_message


def _find_sent_folder(imap) -> Optional[str]:
    """
    Find the Sent folder name for this IMAP server.
    Different providers use different names.
    """
    # Common sent folder names across providers
    sent_folder_names = [
        'Sent',
        'INBOX.Sent',
        'Sent Items',
        'Sent Messages',
        '[Gmail]/Sent Mail',
        'INBOX/Sent',
    ]

    try:
        result, folders = imap.list()
        if result != 'OK':
            return None

        available_folders = []
        for folder_data in folders:
            if folder_data is None:
                continue
            if isinstance(folder_data, bytes):
                # Parse folder name from response like: (\HasNoChildren) "/" "Sent"
                decoded = folder_data.decode('utf-8', errors='replace')
                # Extract folder name (last quoted string or last part)
                if '"' in decoded:
                    parts = decoded.split('"')
                    if len(parts) >= 2:
                        folder_name = decode_imap_utf7(parts[-2])
                        if folder_name:
                            available_folders.append(folder_name)

        # Try to find a matching sent folder
        for sent_name in sent_folder_names:
            if sent_name in available_folders:
                return sent_name
            # Case-insensitive match
            for folder in available_folders:
                if folder.lower() == sent_name.lower():
                    return folder

        # Try partial match for 'sent' keyword
        for folder in available_folders:
            if 'sent' in folder.lower():
                return folder

    except Exception as e:
        logger.warning(f"Error finding sent folder: {e}")

    return None


def _sync_folder(imap, connection, imap_folder_name: str, db_folder_name: str, max_messages: int) -> int:
    """
    Sync messages from a specific IMAP folder.

    Args:
        imap: IMAP connection object
        connection: EmailConnection model instance
        imap_folder_name: Raw folder name for IMAP operations (may be UTF-7 encoded)
        db_folder_name: Decoded folder name for storing in database
        max_messages: Maximum messages to sync per folder

    Returns:
        Number of new messages synced from this folder
    """
    from .models import EmailMessage
    import re as re_module

    try:
        # Quote folder name for IMAP (handles spaces and special chars)
        quoted_folder = f'"{imap_folder_name}"'
        result, data = imap.select(quoted_folder, readonly=True)
        if result != 'OK':
            logger.warning(f"[EMAIL_SYNC] Failed to select folder '{db_folder_name}': {result}")
            return 0

        # Log folder selection and message count
        msg_count = int(data[0]) if data and data[0] else 0
        logger.info(f"[EMAIL_SYNC] Selected folder '{db_folder_name}' - {msg_count} total messages on server")
    except Exception as e:
        logger.warning(f"[EMAIL_SYNC] Error selecting folder '{db_folder_name}': {e}")
        return 0

    # Search for messages - use date filter only if sync_days_back > 0
    if connection.sync_days_back > 0:
        since_date = (timezone.now() - timedelta(days=connection.sync_days_back)).strftime('%d-%b-%Y')
        logger.info(f"[EMAIL_SYNC] Searching '{db_folder_name}' for messages since {since_date} ({connection.sync_days_back} days)")
        result, data = imap.search(None, f'(SINCE {since_date})')
    else:
        # sync_days_back = 0 means sync ALL history
        logger.info(f"[EMAIL_SYNC] Searching '{db_folder_name}' for ALL messages (sync_days_back=0)")
        result, data = imap.search(None, 'ALL')

    if result != 'OK':
        logger.warning(f"[EMAIL_SYNC] Failed to search emails in '{db_folder_name}': {result}")
        return 0

    message_nums = data[0].split()
    total_found = len(message_nums)
    logger.info(f"[EMAIL_SYNC] Found {total_found} messages in '{db_folder_name}' matching search criteria")

    # Limit to most recent messages
    if len(message_nums) > max_messages:
        message_nums = message_nums[-max_messages:]
        logger.info(f"[EMAIL_SYNC] Limited to {len(message_nums)} most recent messages (max_messages={max_messages})")

    if not message_nums:
        return 0

    # Phase 1: Fetch all messages and extract their data
    fetched_messages = []
    message_id_headers = []

    for msg_num in message_nums:
        try:
            result, msg_data = imap.fetch(msg_num, '(RFC822 FLAGS UID)')
            if result != 'OK':
                continue

            raw_email = msg_data[0][1]
            email_message = email.message_from_bytes(raw_email)

            # Parse Message-ID
            message_id_header = email_message.get('Message-ID', '')
            if not message_id_header:
                message_id_header = make_msgid()

            message_id_headers.append(message_id_header)
            fetched_messages.append({
                'msg_num': msg_num,
                'msg_data': msg_data,
                'email_message': email_message,
                'message_id_header': message_id_header,
            })
        except Exception as e:
            logger.warning(f"[EMAIL_SYNC] Failed to fetch email {msg_num} in folder '{db_folder_name}': {e}")
            continue

    if not fetched_messages:
        return 0

    # Phase 2: Batch query for existing messages (single query instead of N queries)
    existing_messages = EmailMessage.objects.filter(
        message_id__in=message_id_headers
    ).values('message_id', 'folder', 'id')

    # Create lookup dict for O(1) access
    existing_lookup = {msg['message_id']: msg for msg in existing_messages}

    # Track messages that need folder updates (batch update later)
    messages_to_update_folder = []

    # Phase 3: Process messages and create new ones
    new_count = 0
    messages_to_create = []

    for msg_info in fetched_messages:
        try:
            message_id_header = msg_info['message_id_header']
            email_message = msg_info['email_message']
            msg_data = msg_info['msg_data']

            # Check if already exists using lookup dict (O(1) instead of DB query)
            existing = existing_lookup.get(message_id_header)
            if existing:
                # Update folder if it changed (email was moved on server)
                if existing['folder'] != db_folder_name:
                    logger.info(f"[EMAIL_SYNC] Updating folder '{existing['folder']}' -> '{db_folder_name}' for message_id={message_id_header[:50]}")
                    messages_to_update_folder.append(existing['id'])
                else:
                    logger.debug(f"[EMAIL_SYNC] Message already synced in correct folder: {message_id_header[:50]}")
                continue

            # Parse sender
            from_header = email_message.get('From', '')
            from_name, from_email_addr = parseaddr(from_header)
            from_name = decode_mime_header(from_name)  # Decode MIME-encoded sender names

            # Parse recipients
            to_emails = parse_address_list(email_message.get('To', ''))
            cc_emails = parse_address_list(email_message.get('Cc', ''))

            # Parse date
            date_str = email_message.get('Date')
            try:
                timestamp = parsedate_to_datetime(date_str)
                if timestamp.tzinfo is None:
                    timestamp = timezone.make_aware(timestamp)
            except Exception:
                timestamp = timezone.now()

            # Get body
            body_text, body_html = extract_body(email_message)

            # Get attachments
            message_attachments = extract_attachments(email_message, connection)

            # Thread ID
            references = email_message.get('References', '')
            in_reply_to = email_message.get('In-Reply-To', '')
            thread_id = compute_thread_id(message_id_header, in_reply_to, references)

            # Determine if from business
            is_from_business = from_email_addr.lower() == connection.email_address.lower()

            # Parse IMAP flags
            flags_data = msg_data[0][0] if len(msg_data[0]) > 0 else b''
            flags = imaplib.ParseFlags(flags_data) if flags_data else ()

            # Get UID
            uid = ''
            uid_match = msg_data[0][0].decode() if isinstance(msg_data[0][0], bytes) else str(msg_data[0][0])
            if 'UID' in uid_match:
                uid_search = re_module.search(r'UID (\d+)', uid_match)
                if uid_search:
                    uid = uid_search.group(1)

            # Create message object (will be bulk created)
            messages_to_create.append(EmailMessage(
                connection=connection,
                message_id=message_id_header,
                thread_id=thread_id,
                in_reply_to=in_reply_to,
                references=references,
                from_email=from_email_addr,
                from_name=from_name,
                to_emails=to_emails,
                cc_emails=cc_emails,
                subject=decode_mime_header(email_message.get('Subject', '')),
                body_text=body_text,
                body_html=body_html,
                attachments=message_attachments,
                timestamp=timestamp,
                folder=db_folder_name,
                uid=uid,
                is_from_business=is_from_business,
                is_read=b'\\Seen' in flags,
                is_starred=b'\\Flagged' in flags,
                is_answered=b'\\Answered' in flags,
            ))
            new_count += 1
            logger.info(f"[EMAIL_SYNC] Prepared new message in folder '{db_folder_name}': {email_message.get('Subject', '')[:50]}")

        except Exception as e:
            logger.warning(f"[EMAIL_SYNC] Failed to process email {msg_info['msg_num']} in folder '{db_folder_name}': {e}")
            continue

    # Batch create new messages (single INSERT instead of N INSERTs)
    if messages_to_create:
        EmailMessage.objects.bulk_create(messages_to_create, ignore_conflicts=True)
        logger.info(f"[EMAIL_SYNC] Bulk created {len(messages_to_create)} messages")

    # Batch update folder for moved messages (single UPDATE instead of N UPDATEs)
    if messages_to_update_folder:
        EmailMessage.objects.filter(id__in=messages_to_update_folder).update(folder=db_folder_name)
        logger.info(f"[EMAIL_SYNC] Batch updated folder for {len(messages_to_update_folder)} messages")

    logger.info(f"[EMAIL_SYNC] Folder '{db_folder_name}' sync complete: {new_count} new messages created")
    return new_count


def _connect_imap(connection):
    """
    Create and authenticate an IMAP connection.

    Args:
        connection: EmailConnection model instance

    Returns:
        Authenticated IMAP connection object
    """
    if connection.imap_use_ssl:
        imap = imaplib.IMAP4_SSL(connection.imap_server, connection.imap_port, timeout=30)
    else:
        imap = imaplib.IMAP4(connection.imap_server, connection.imap_port)
        imap.starttls()

    _imap_authenticate(imap, connection.username, connection.get_password())
    return imap


SYNC_FAILURE_THRESHOLD = 5


def _auto_disable_email_connection(connection, error_message: str) -> None:
    """Flip a connection to disabled after a sync failure.

    Triggered either by a non-transient error on a single attempt, or by
    repeated transient errors accumulating past ``SYNC_FAILURE_THRESHOLD``.

    - Sets ``is_active=False`` so subsequent Celery runs skip it.
    - Stamps ``auto_disabled_at`` so the frontend can show "disabled by system"
      and force the user through the Reactivate flow (which re-tests creds).
    - Resets ``sync_failure_count`` to 0 — reactivate starts from a clean slate.
    - Notifies tenant admins so the outage doesn't go silent.

    Safe to call when the connection is already auto-disabled — we skip the
    notification burst in that case (sync will still reset ``last_sync_error``).
    """
    was_already_disabled = connection.auto_disabled_at is not None
    connection.is_active = False
    connection.auto_disabled_at = timezone.now()
    connection.last_sync_error = error_message
    connection.sync_failure_count = 0
    connection.save(update_fields=[
        'is_active', 'auto_disabled_at', 'last_sync_error', 'sync_failure_count'
    ])

    logger.warning(
        f"[EMAIL_SYNC] Auto-disabled {connection.email_address} — {error_message}"
    )

    if was_already_disabled:
        return  # Don't spam admins on every retry cycle

    # Notify tenant admins so the outage surfaces in-app.
    try:
        from django.contrib.auth import get_user_model
        from users.notification_utils import create_notification
        User = get_user_model()
        admins = User.objects.filter(is_active=True).filter(
            is_staff=True
        ) | User.objects.filter(is_active=True, is_superuser=True)
        admins = admins.distinct()
        for admin in admins:
            create_notification(
                user=admin,
                notification_type='email_sync_disabled',
                title=f'Email sync disabled: {connection.email_address}',
                message=(
                    f'Automatic IMAP sync for {connection.email_address} was '
                    f'disabled after an error. Go to email settings to fix '
                    f'credentials and reactivate. Error: {error_message[:200]}'
                ),
                metadata={
                    'connection_id': connection.id,
                    'email_address': connection.email_address,
                    'error': error_message[:500],
                },
                link_url='/settings/social/email',
            )
    except Exception:
        # Never let notification dispatch break the sync worker.
        logger.exception(
            f"[EMAIL_SYNC] Failed to notify admins about auto-disabled "
            f"{connection.email_address}"
        )


def sync_imap_messages(connection, max_messages: int = 500) -> int:
    """
    Sync messages from IMAP server for a given connection.
    Syncs both the configured folder (usually INBOX) and the Sent folder.

    If the IMAP connection drops mid-sync (e.g. "connection already closed"),
    the function will automatically reconnect and retry the failed folder
    (up to 2 retries per folder).

    Args:
        connection: EmailConnection model instance
        max_messages: Maximum number of messages to fetch per sync per folder

    Returns:
        Number of new messages synced
    """
    try:
        # Connect to IMAP
        imap = _connect_imap(connection)

        total_new_count = 0

        # Get all available folders
        result, folders_data = imap.list()
        if result == 'OK':
            all_folders = []
            folders_to_sync = []
            skipped_folders = []

            for folder_data in folders_data:
                if folder_data is None:
                    continue
                if isinstance(folder_data, bytes):
                    decoded = folder_data.decode('utf-8', errors='replace')
                    # Extract folder name from response like: (\HasNoChildren) "/" "INBOX"
                    if '"' in decoded:
                        parts = decoded.split('"')
                        if len(parts) >= 2:
                            raw_folder_name = parts[-2]  # Keep raw for IMAP operations
                            display_folder_name = decode_imap_utf7(raw_folder_name) or raw_folder_name  # Decode for display/DB
                            all_folders.append(display_folder_name)
                            # Skip Drafts, All Mail, Trash, and Spam. Sent IS synced
                            # (normalized to the 'Sent' DB folder) so replies the
                            # business sends from an external email client are
                            # detectable — EchoDesk-sent mail auto-saved to the
                            # server's Sent folder dedupes on the unique
                            # message_id, since send_email_smtp stores the real
                            # Message-ID header.
                            skip_folders = ['Drafts', '[Gmail]/Drafts', '[Gmail]/All Mail', 'Trash', '[Gmail]/Trash', 'Deleted', 'Deleted Items', 'Deleted Messages', 'Spam', '[Gmail]/Spam', 'Junk', 'Junk E-mail']
                            sent_folders = ['Sent', '[Gmail]/Sent Mail', 'Sent Items', 'Sent Messages']
                            if any(skip.lower() == display_folder_name.lower() for skip in skip_folders):
                                skipped_folders.append(display_folder_name)
                            elif any(sent.lower() == display_folder_name.lower() for sent in sent_folders):
                                # Normalize provider-specific sent folder names so
                                # all business-sent mail lives in folder='Sent'.
                                folders_to_sync.append((raw_folder_name, 'Sent'))
                            else:
                                # Store tuple of (raw_name, display_name)
                                folders_to_sync.append((raw_folder_name, display_folder_name))

            # Log folder discovery details
            logger.info(f"[EMAIL_SYNC] ===== FOLDER DISCOVERY for {connection.email_address} =====")
            logger.info(f"[EMAIL_SYNC] All folders on server ({len(all_folders)}): {all_folders}")
            logger.info(f"[EMAIL_SYNC] Folders to sync ({len(folders_to_sync)}): {[f[1] for f in folders_to_sync]}")
            logger.info(f"[EMAIL_SYNC] Skipped folders ({len(skipped_folders)}): {skipped_folders}")
            logger.info(f"[EMAIL_SYNC] sync_days_back setting: {connection.sync_days_back} (0=all history)")
            logger.info(f"[EMAIL_SYNC] ================================================")

            # Sync each folder with automatic reconnection on connection drop
            for raw_folder_name, display_folder_name in folders_to_sync:
                max_retries = 2
                for attempt in range(max_retries + 1):
                    try:
                        folder_count = _sync_folder(imap, connection, raw_folder_name, display_folder_name, max_messages)
                        total_new_count += folder_count
                        if folder_count > 0:
                            logger.info(f"Synced {folder_count} emails from {display_folder_name}")
                        break  # Success, move to next folder
                    except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, ConnectionError, BrokenPipeError) as e:
                        error_msg = str(e).lower()
                        is_connection_error = any(keyword in error_msg for keyword in [
                            'connection already closed', 'socket error', 'broken pipe',
                            'connection reset', 'eof', 'timed out', 'bad file descriptor',
                        ])
                        if is_connection_error and attempt < max_retries:
                            logger.warning(
                                f"[EMAIL_SYNC] Connection dropped while syncing folder '{display_folder_name}' "
                                f"(attempt {attempt + 1}/{max_retries + 1}): {e}. Reconnecting..."
                            )
                            try:
                                imap = _connect_imap(connection)
                                logger.info(f"[EMAIL_SYNC] Reconnected to IMAP for {connection.email_address}")
                            except Exception as reconnect_err:
                                logger.error(f"[EMAIL_SYNC] Failed to reconnect IMAP: {reconnect_err}")
                                raise  # Give up if we can't reconnect
                        else:
                            logger.warning(f"Failed to sync folder {display_folder_name}: {e}")
                            break  # Non-connection error or exhausted retries, skip folder
                    except Exception as e:
                        logger.warning(f"Failed to sync folder {display_folder_name}: {e}")
                        break  # Unknown error, skip folder

        try:
            imap.logout()
        except Exception:
            pass  # Connection may already be closed

        # Update connection status — success clears the error + failure counter
        connection.last_sync_at = timezone.now()
        connection.last_sync_error = ''
        connection.sync_failure_count = 0
        connection.save(update_fields=['last_sync_at', 'last_sync_error', 'sync_failure_count'])

        logger.info(f"Synced {total_new_count} total new emails for {connection.email_address}")
        return total_new_count

    except (ssl.SSLError, socket.error, OSError, imaplib.IMAP4.abort) as e:
        # Transient SSL/socket errors (e.g. BAD_LENGTH, EOF violations, "connection
        # reset by peer"). Normally we retry next cycle — but if the same class of
        # error keeps happening, the server is effectively refusing us and retrying
        # forever won't help. Track consecutive failures and auto-disable past the
        # threshold so the user sees the outage.
        connection.sync_failure_count = (connection.sync_failure_count or 0) + 1
        failure_count = connection.sync_failure_count

        if failure_count >= SYNC_FAILURE_THRESHOLD:
            logger.warning(
                f"[EMAIL_SYNC] {connection.email_address} hit "
                f"{failure_count} consecutive transient errors; auto-disabling. "
                f"Last error: {e}"
            )
            _auto_disable_email_connection(
                connection,
                f"Disabled after {failure_count} consecutive failures. Last error: {e}",
            )
        else:
            logger.warning(
                f"Transient email sync error for {connection.email_address} "
                f"({failure_count}/{SYNC_FAILURE_THRESHOLD}): {e}"
            )
            connection.last_sync_error = str(e)
            connection.save(update_fields=['last_sync_error', 'sync_failure_count'])
        return 0
    except Exception as e:
        # Non-transient (auth failure, host resolution, permission, etc.) —
        # disable immediately. The user must fix credentials and click
        # Reactivate in the UI to resume sync.
        logger.error(f"Email sync error for {connection.email_address}: {e}")
        _auto_disable_email_connection(connection, str(e))
        return 0


def delete_emails_from_imap(connection, message_ids: List[str]) -> int:
    """
    Delete emails from IMAP server by moving them to Trash.

    Args:
        connection: EmailConnection model instance
        message_ids: List of Message-ID headers to delete

    Returns:
        Number of emails deleted
    """
    if not message_ids:
        return 0

    try:
        # Connect to IMAP
        if connection.imap_use_ssl:
            imap = imaplib.IMAP4_SSL(connection.imap_server, connection.imap_port, timeout=30)
        else:
            imap = imaplib.IMAP4(connection.imap_server, connection.imap_port)
            imap.starttls()

        _imap_authenticate(imap, connection.username, connection.get_password())

        # Select folder (not readonly - we need to modify)
        result, data = imap.select(connection.sync_folder, readonly=False)
        if result != 'OK':
            raise Exception(f"Failed to select folder {connection.sync_folder}")

        deleted_count = 0

        for message_id in message_ids:
            try:
                # Search for message by Message-ID header
                # Note: Message-ID includes angle brackets, search needs them
                search_id = message_id if message_id.startswith('<') else f'<{message_id}>'
                result, data = imap.search(None, f'HEADER Message-ID "{search_id}"')

                if result != 'OK' or not data[0]:
                    # Try without angle brackets
                    clean_id = message_id.strip('<>')
                    result, data = imap.search(None, f'HEADER Message-ID "{clean_id}"')

                if result == 'OK' and data[0]:
                    msg_nums = data[0].split()
                    for msg_num in msg_nums:
                        # Mark as deleted
                        imap.store(msg_num, '+FLAGS', '\\Deleted')
                        deleted_count += 1

            except Exception as e:
                logger.warning(f"Failed to delete email {message_id}: {e}")
                continue

        # Expunge to permanently remove deleted messages
        imap.expunge()

        imap.close()
        imap.logout()

        logger.info(f"Deleted {deleted_count} emails from IMAP for {connection.email_address}")
        return deleted_count

    except Exception as e:
        logger.error(f"IMAP delete error for {connection.email_address}: {e}")
        raise


def move_email_to_folder(connection, message_id: str, source_folder: str, target_folder: str) -> bool:
    """
    Move an email from one IMAP folder to another.

    Args:
        connection: EmailConnection model instance
        message_id: Message-ID header of the email to move
        source_folder: Current folder of the email
        target_folder: Destination folder

    Returns:
        True if successful, False otherwise
    """
    try:
        # Connect to IMAP
        if connection.imap_use_ssl:
            imap = imaplib.IMAP4_SSL(connection.imap_server, connection.imap_port, timeout=30)
        else:
            imap = imaplib.IMAP4(connection.imap_server, connection.imap_port)
            imap.starttls()

        _imap_authenticate(imap, connection.username, connection.get_password())

        # Select source folder
        result, data = imap.select(source_folder, readonly=False)
        if result != 'OK':
            raise Exception(f"Failed to select source folder {source_folder}")

        # Search for message by Message-ID
        search_id = message_id if message_id.startswith('<') else f'<{message_id}>'
        result, data = imap.search(None, f'HEADER Message-ID "{search_id}"')

        if result != 'OK' or not data[0]:
            # Try without angle brackets
            clean_id = message_id.strip('<>')
            result, data = imap.search(None, f'HEADER Message-ID "{clean_id}"')

        if result != 'OK' or not data[0]:
            logger.warning(f"Message {message_id} not found in {source_folder}")
            imap.logout()
            return False

        msg_nums = data[0].split()
        if not msg_nums:
            imap.logout()
            return False

        msg_num = msg_nums[0]

        # Copy to target folder
        result, data = imap.copy(msg_num, target_folder)
        if result != 'OK':
            raise Exception(f"Failed to copy message to {target_folder}")

        # Mark original as deleted
        imap.store(msg_num, '+FLAGS', '\\Deleted')

        # Expunge to remove the original
        imap.expunge()

        imap.close()
        imap.logout()

        logger.info(f"Moved email {message_id} from {source_folder} to {target_folder}")
        return True

    except Exception as e:
        logger.error(f"IMAP move error: {e}")
        return False


def get_available_folders(connection) -> List[Dict[str, Any]]:
    """
    Get list of available IMAP folders for a connection.

    Returns:
        List of folder info dicts with name and flags
    """
    try:
        if connection.imap_use_ssl:
            imap = imaplib.IMAP4_SSL(connection.imap_server, connection.imap_port, timeout=30)
        else:
            imap = imaplib.IMAP4(connection.imap_server, connection.imap_port)
            imap.starttls()

        _imap_authenticate(imap, connection.username, connection.get_password())

        result, folders_data = imap.list()
        folders = []

        if result == 'OK':
            for folder_data in folders_data:
                if folder_data is None:
                    continue
                if isinstance(folder_data, bytes):
                    decoded = folder_data.decode('utf-8', errors='replace')
                    if '"' in decoded:
                        parts = decoded.split('"')
                        if len(parts) >= 2:
                            folder_name = decode_imap_utf7(parts[-2])
                            if not folder_name:
                                continue
                            # Skip Drafts, All Mail, Trash, Sent, and Spam
                            skip_folders = ['Drafts', '[Gmail]/Drafts', '[Gmail]/All Mail', 'Trash', '[Gmail]/Trash', 'Deleted', 'Deleted Items', 'Deleted Messages', 'Sent', '[Gmail]/Sent Mail', 'Sent Items', 'Sent Messages', 'Spam', '[Gmail]/Spam', 'Junk', 'Junk E-mail']
                            if not any(skip.lower() == folder_name.lower() for skip in skip_folders):
                                folders.append({
                                    'name': folder_name,
                                    'display_name': folder_name.replace('[Gmail]/', '').replace('INBOX/', '')
                                })

        imap.logout()
        return folders

    except Exception as e:
        logger.error(f"Error getting folders: {e}")
        return []


def debug_email_sync(connection) -> Dict[str, Any]:
    """
    Debug email sync - returns detailed folder info without actually syncing.

    Args:
        connection: EmailConnection model instance

    Returns:
        Dict with debug info about folders, message counts, and settings
    """
    from .models import EmailMessage

    debug_info = {
        'connection': {
            'email': connection.email_address,
            'imap_server': connection.imap_server,
            'sync_days_back': connection.sync_days_back,
            'sync_days_back_meaning': 'all history' if connection.sync_days_back == 0 else f'{connection.sync_days_back} days',
            'last_sync_at': str(connection.last_sync_at) if connection.last_sync_at else None,
            'last_sync_error': connection.last_sync_error or None,
        },
        'folders': [],
        'skipped_folders': [],
        'summary': {},
        'errors': []
    }

    try:
        # Connect to IMAP
        if connection.imap_use_ssl:
            imap = imaplib.IMAP4_SSL(connection.imap_server, connection.imap_port, timeout=30)
        else:
            imap = imaplib.IMAP4(connection.imap_server, connection.imap_port)
            imap.starttls()

        _imap_authenticate(imap, connection.username, connection.get_password())

        # Get all folders
        result, folders_data = imap.list()
        if result != 'OK':
            debug_info['errors'].append(f"Failed to list folders: {result}")
            return debug_info

        skip_folders = ['Drafts', '[Gmail]/Drafts', '[Gmail]/All Mail', 'Trash', '[Gmail]/Trash',
                       'Deleted', 'Deleted Items', 'Deleted Messages', 'Sent', '[Gmail]/Sent Mail',
                       'Sent Items', 'Sent Messages', 'Spam', '[Gmail]/Spam', 'Junk', 'Junk E-mail']

        for folder_data in folders_data:
            if folder_data is None:
                continue
            if isinstance(folder_data, bytes):
                decoded = folder_data.decode('utf-8', errors='replace')
                if '"' in decoded:
                    parts = decoded.split('"')
                    if len(parts) >= 2:
                        raw_folder_name = parts[-2]  # Keep raw for IMAP
                        display_folder_name = decode_imap_utf7(raw_folder_name) or raw_folder_name  # Decode for display

                        # Check if skipped
                        is_skipped = any(skip.lower() == display_folder_name.lower() for skip in skip_folders)

                        folder_info = {
                            'name': display_folder_name,
                            'name_bytes': display_folder_name.encode('utf-8').hex(),  # For debugging encoding issues
                            'is_synced': not is_skipped,
                            'skip_reason': 'In skip list' if is_skipped else None,
                        }

                        if not is_skipped:
                            # Get message count and search info for this folder
                            try:
                                # Use raw folder name for IMAP operations
                                quoted_folder = f'"{raw_folder_name}"'
                                result, data = imap.select(quoted_folder, readonly=True)
                                if result == 'OK':
                                    total_messages = int(data[0]) if data and data[0] else 0
                                    folder_info['total_messages_on_server'] = total_messages

                                    # Search with date filter
                                    if connection.sync_days_back > 0:
                                        since_date = (timezone.now() - timedelta(days=connection.sync_days_back)).strftime('%d-%b-%Y')
                                        result, data = imap.search(None, f'(SINCE {since_date})')
                                        folder_info['search_filter'] = f'SINCE {since_date}'
                                    else:
                                        result, data = imap.search(None, 'ALL')
                                        folder_info['search_filter'] = 'ALL'

                                    if result == 'OK':
                                        message_ids = data[0].split() if data[0] else []
                                        folder_info['messages_matching_filter'] = len(message_ids)
                                    else:
                                        folder_info['search_error'] = str(result)

                                    # Count already synced messages in DB for this folder (using display name)
                                    synced_count = EmailMessage.objects.filter(
                                        connection=connection,
                                        folder=display_folder_name
                                    ).count()
                                    folder_info['messages_already_synced'] = synced_count
                                else:
                                    folder_info['select_error'] = str(result)
                            except Exception as e:
                                folder_info['error'] = str(e)

                            debug_info['folders'].append(folder_info)
                        else:
                            debug_info['skipped_folders'].append(folder_info)

        imap.logout()

        # Summary
        debug_info['summary'] = {
            'total_folders_on_server': len(debug_info['folders']) + len(debug_info['skipped_folders']),
            'folders_being_synced': len(debug_info['folders']),
            'folders_skipped': len(debug_info['skipped_folders']),
            'total_messages_in_db': EmailMessage.objects.filter(connection=connection).count(),
        }

    except Exception as e:
        debug_info['errors'].append(str(e))

    return debug_info
