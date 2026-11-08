"""
Email utility functions for IMAP/SMTP operations.
"""
import imaplib
import re
import smtplib
import ssl
import socket
import email
import hashlib
import logging
import base64
import copy
import time
import uuid
from urllib.parse import unquote, urlparse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import make_msgid, formataddr, formatdate, parseaddr, getaddresses, parsedate_to_datetime
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


_LIST_LINE_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$', re.IGNORECASE)


def parse_imap_list_entry(folder_data) -> Optional[str]:
    """
    Return the raw (still modified-UTF-7) mailbox name from one IMAP LIST
    response entry, or None if it can't be parsed.

    Handles the three shapes servers send:
      (\\HasNoChildren) "/" "INBOX"      quoted name (Gmail, Outlook)
      (\\HasNoChildren) "/" INBOX        atom name (Dovecot/cPanel hosts)
      ((\\HasNoChildren) "/" {12}, b'Name with "') literal, as a tuple
    """
    if folder_data is None:
        return None
    if isinstance(folder_data, tuple):
        # imaplib returns a literal as (b'<flags> <delim> {n}', b'<name>')
        if len(folder_data) < 2 or not isinstance(folder_data[1], bytes):
            return None
        return folder_data[1].decode('utf-8', errors='replace') or None
    if not isinstance(folder_data, bytes):
        return None
    decoded = folder_data.decode('utf-8', errors='replace').strip()
    match = _LIST_LINE_RE.match(decoded)
    if not match:
        return None
    name = match.group('name').strip()
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        name = re.sub(r'\\(.)', r'\1', name[1:-1])
    return name or None


def imap_list_flags(folder_data) -> set:
    """Lower-cased mailbox flags (e.g. '\\sent', '\\trash') from a LIST entry."""
    if isinstance(folder_data, tuple):
        folder_data = folder_data[0] if folder_data else None
    if not isinstance(folder_data, bytes):
        return set()
    match = _LIST_LINE_RE.match(folder_data.decode('utf-8', errors='replace').strip())
    if not match:
        return set()
    return {flag.lower() for flag in match.group('flags').split()}


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
        details = []
        if result == 'OK':
            from .email_mailbox import mailboxes_from_list_data
            for folder_data in folder_list:
                raw_name = parse_imap_list_entry(folder_data)
                if raw_name:
                    folders.append(decode_imap_utf7(raw_name))
            # Selectable folders with role + the DB name messages are stored under.
            details = [
                {'name': e['db_name'], 'display_name': e['name'], 'raw_name': e['raw_name'], 'role': e['role']}
                for e in mailboxes_from_list_data(folder_list)
            ]

        return {'success': True, 'folders': folders, 'details': details}
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


def _storage_safe_filename(filename: str) -> str:
    """Object-storage key for an attachment. Keys built from raw mail
    filenames (non-ASCII, "?", "=", "/", control chars) were rejected by
    the S3 PutObject call with BadRequest, so the key gets an ASCII-only
    name plus a short random prefix; the real filename is kept in the
    attachment dict for display."""
    import os
    import unicodedata
    base, ext = os.path.splitext(os.path.basename(filename.replace('\\', '/')))
    def _ascii(value: str) -> str:
        value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
        return re.sub(r'[^A-Za-z0-9._-]+', '-', value).strip('.-_')
    base = _ascii(base)[:80] or 'attachment'
    ext = _ascii(ext)[:16]
    return f"{uuid.uuid4().hex[:8]}-{base}{'.' + ext if ext else ''}"


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

        # get_filename() leaves RFC 2047 words ("=?utf-8?B?...?=") encoded
        # when the sender didn't use RFC 2231; decode them for display.
        filename = decode_mime_header(part.get_filename() or '').strip()
        if not filename:
            # Generate filename for inline content
            ext = part.get_content_type().split('/')[-1]
            filename = f"attachment.{ext}"

        content_type = part.get_content_type()

        try:
            payload = part.get_payload(decode=True)
            if payload:
                # Save to storage
                file_path = (
                    f'email_attachments/{connection.id}/{timezone.now().strftime("%Y/%m/%d")}/'
                    f'{_storage_safe_filename(filename)}'
                )
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


# <img src="data:image/...;base64,..."> — Gmail and Outlook strip data: URIs
# from received mail, so a signature logo pasted as base64 arrives as a blank
# box. Before sending they are moved into multipart/related parts and the img
# points at them with cid: (which every client renders inline).
_DATA_IMG_RE = re.compile(
    r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])data:(image/(?:png|jpe?g|gif|webp|bmp));base64,([A-Za-z0-9+/=\s]+)\2""",
    re.IGNORECASE,
)
MAX_INLINE_IMAGES = 20
MAX_INLINE_IMAGE_BYTES = 2 * 1024 * 1024


def inline_data_images(html: str, domain: Optional[str] = None):
    """Replace base64 ``data:`` images in ``html`` with ``cid:`` references.

    Returns ``(html, parts)`` where ``parts`` are ``MIMEImage``-style parts
    (Content-ID + inline disposition) to attach next to the HTML inside a
    ``multipart/related``. Identical images share one part; undecodable or
    oversized ones, and anything past ``MAX_INLINE_IMAGES``, are left as is.
    """
    from email.mime.image import MIMEImage

    if not html or 'data:' not in html:
        return html, []
    parts = []
    by_digest = {}

    def replace(match):
        mime, payload = match.group(3).lower(), re.sub(r'\s+', '', match.group(4))
        try:
            data = base64.b64decode(payload, validate=True)
        except (ValueError, base64.binascii.Error):
            return match.group(0)
        if not data or len(data) > MAX_INLINE_IMAGE_BYTES:
            return match.group(0)
        digest = hashlib.sha256(data).hexdigest()
        cid = by_digest.get(digest)
        if cid is None:
            if len(parts) >= MAX_INLINE_IMAGES:
                return match.group(0)
            cid = make_msgid(idstring=f'img{len(parts) + 1}', domain=domain or 'echodesk.ge')[1:-1]
            subtype = mime.split('/', 1)[1].replace('jpg', 'jpeg')
            part = MIMEImage(data, _subtype=subtype)
            part.add_header('Content-ID', f'<{cid}>')
            part.add_header('Content-Disposition', 'inline', filename=f'image{len(parts) + 1}.{subtype}')
            parts.append(part)
            by_digest[digest] = cid
        return f'{match.group(1)}{match.group(2)}cid:{cid}{match.group(2)}'

    return _DATA_IMG_RE.sub(replace, html), parts


def externalize_data_images(html: str, folder: str = 'email_signatures') -> str:
    """Upload base64 ``data:`` images in a saved signature to public storage
    and point the ``<img>`` at the public URL.

    Signatures pasted from another mail client often carry the logo inline as
    base64; stored as a URL they stay small, show in previews and in EchoDesk's
    reader, and (should one slip through) ``inline_data_images`` still covers
    sending. Undecodable or oversized images are left untouched; a storage
    failure keeps the original HTML.
    """
    if not html or 'data:' not in html:
        return html

    def replace(match):
        mime, payload = match.group(3).lower(), re.sub(r'\s+', '', match.group(4))
        try:
            data = base64.b64decode(payload, validate=True)
        except (ValueError, base64.binascii.Error):
            return match.group(0)
        if not data or len(data) > MAX_INLINE_IMAGE_BYTES:
            return match.group(0)
        ext = mime.split('/', 1)[1].replace('jpeg', 'jpg')
        digest = hashlib.sha256(data).hexdigest()[:16]
        try:
            path = default_storage.save(f'{folder}/{digest}.{ext}', ContentFile(data))
            url = default_storage.url(path)
        except Exception as exc:  # pragma: no cover - storage outage
            logger.warning('Could not store signature image: %s', exc)
            return match.group(0)
        return f'{match.group(1)}{match.group(2)}{url}{match.group(2)}'

    return _DATA_IMG_RE.sub(replace, html)


def html_mime_part(html: str, domain: Optional[str] = None):
    """The text/html part, wrapped in multipart/related with its inline images
    when the HTML carries base64 ``data:`` images (see ``inline_data_images``)."""
    html, images = inline_data_images(html, domain)
    html_part = MIMEText(html, 'html', 'utf-8')
    if not images:
        return html_part
    related = MIMEMultipart('related')
    related.attach(html_part)
    for image in images:
        related.attach(image)
    return related


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


# Hosts whose servers file a copy of every SMTP-submitted message into the
# Sent folder on their own; APPENDing there too would duplicate it.
SELF_SAVING_SENT_HOST_MARKERS = (
    'gmail.com', 'googlemail.com', 'office365.com', 'outlook.com',
    'hotmail.com', 'live.com',
)

# References header cap (RFC 5322 suggests trimming long chains).
MAX_REFERENCES = 20


def provider_saves_sent_copy(connection) -> bool:
    """True when the provider stores sent mail in Sent itself (Gmail, O365...).

    Whole-domain match on the SMTP/IMAP host ("smtp.office365.com" yes,
    "mail.olive.com" no).
    """
    for host in (getattr(connection, 'smtp_server', ''), getattr(connection, 'imap_server', '')):
        host = (host or '').strip().lower().rstrip('.')
        if host and any(host == d or host.endswith('.' + d) for d in SELF_SAVING_SENT_HOST_MARKERS):
            return True
    return False


def _signature_html_to_text(html: str) -> str:
    text = re.sub(r'<[^>]+>', '', html or '')
    return text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&').strip()


def get_effective_signature(connection, is_reply: bool = False) -> Dict[str, Any]:
    """The signature send_email_smtp would use for ``connection``.

    Priority: the connection's own signature when ``signature_enabled`` and
    non-empty (always used, replies included), else the tenant EmailSignature
    when ``is_enabled`` (honours ``include_on_reply``), else none.

    Returns ``{source, is_enabled, include_on_reply, signature_html,
    signature_text, applies}`` where ``applies`` says whether a message of the
    given kind (``is_reply``) gets the signature appended. Shared by the send
    path and ``email/status`` so the composer can't drift from what is sent.
    """
    from .models import EmailSignature

    result = {
        'source': 'none',
        'is_enabled': False,
        'include_on_reply': False,
        'signature_html': '',
        'signature_text': '',
    }
    try:
        if connection.signature_enabled and (connection.signature_html or connection.signature_text):
            result.update({
                'source': 'connection',
                'is_enabled': True,
                'include_on_reply': True,
                'signature_html': connection.signature_html or connection.signature_text.replace('\n', '<br>'),
                'signature_text': connection.signature_text or _signature_html_to_text(connection.signature_html),
            })
        else:
            signature = EmailSignature.objects.first()
            if signature and signature.is_enabled:
                html = signature.signature_html or (signature.signature_text or '').replace('\n', '<br>')
                text = signature.signature_text or _signature_html_to_text(signature.signature_html)
                result.update({
                    'source': 'tenant',
                    'is_enabled': True,
                    'include_on_reply': bool(signature.include_on_reply),
                    'signature_html': html or '',
                    'signature_text': text or '',
                })
    except Exception as e:
        logger.warning(f"Failed to get email signature: {e}")

    result['applies'] = bool(result['is_enabled'] and (not is_reply or result['include_on_reply']))
    return result


def _store_outgoing_attachment(connection, filename: str, payload: bytes) -> str:
    """Save an outgoing attachment to storage and return its URL.

    Same layout as extract_attachments plus a random path segment, so sent
    attachment URLs can't be guessed from the date and file name.
    """
    safe_name = (filename or 'file').replace('/', '_').replace('\\', '_')
    file_path = (
        f'email_attachments/{connection.id}/{timezone.now().strftime("%Y/%m/%d")}/'
        f'{uuid.uuid4().hex}/{safe_name}'
    )
    saved_path = default_storage.save(file_path, ContentFile(payload))
    return default_storage.url(saved_path)


def _storage_name_from_url(url: str) -> Optional[str]:
    """Map a default_storage URL (as stored in EmailMessage.attachments) back to its name."""
    if not url:
        return None
    path = unquote(urlparse(url).path).lstrip('/')
    media_prefix = urlparse(settings.MEDIA_URL or '').path.lstrip('/')
    if media_prefix and path.startswith(media_prefix):
        path = path[len(media_prefix):]
    return path or None


def load_message_attachments(message) -> List[Dict[str, Any]]:
    """Read a stored message's attachments back from storage for re-sending.

    Returns send_email_smtp-style dicts ``{filename, content, content_type}``.
    Attachments whose file can't be read are skipped (logged), never fetched
    from arbitrary URLs.
    """
    loaded = []
    for att in (message.attachments or []):
        if not isinstance(att, dict):
            continue
        name = _storage_name_from_url(att.get('url', ''))
        if not name:
            continue
        try:
            with default_storage.open(name, 'rb') as fh:
                content = fh.read()
        except Exception as e:
            logger.warning(f"Could not load attachment {name} of message {message.pk}: {e}")
            continue
        loaded.append({
            'filename': att.get('filename') or name.rsplit('/', 1)[-1],
            'content': content,
            'content_type': att.get('content_type') or 'application/octet-stream',
        })
    return loaded


def build_references(original) -> str:
    """References for a reply: parent chain + parent Message-ID, deduped, capped."""
    chain = (original.references or original.in_reply_to or '').split()
    chain.append(original.message_id or '')
    ids = []
    for ref in chain:
        ref = ref.strip()
        if ref and ref not in ids:
            ids.append(ref)
    if len(ids) > MAX_REFERENCES:
        # Keep the thread root plus the most recent ids.
        ids = ids[:1] + ids[-(MAX_REFERENCES - 1):]
    return ' '.join(ids)


def _dedupe_addresses(addresses: List[str], seen: set) -> List[str]:
    out = []
    for addr in addresses or []:
        key = (addr or '').strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(addr.strip())
    return out


def send_email_smtp(connection, to_emails: List[str], cc_emails: List[str] = None,
                    bcc_emails: List[str] = None, subject: str = '',
                    body_text: str = '', body_html: str = '',
                    reply_to_message_id: str = None, attachments: List[Dict] = None,
                    signature_included: bool = False, reply_to_message=None,
                    forward_message=None, report: Optional[Dict[str, Any]] = None) -> Tuple[str, Any]:
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
        attachments: List of {filename, content, content_type} dicts
        signature_included: the composer already put the signature in the body;
            don't append one.
        reply_to_message: EmailMessage being replied to (preferred over the
            Message-ID lookup). Marked answered after the send.
        forward_message: EmailMessage being forwarded; gets $Forwarded on the
            server after the send.
        report: optional dict filled with ``refused_recipients`` (list) and
            ``warnings`` (list of str).

    Returns:
        Tuple of (message_id, saved_EmailMessage). The saved row is None when
        the mail went out but storing it failed (see ``report['warnings']``).

    After a successful send the exact MIME (with Bcc) is IMAP-APPENDed to the
    server's Sent folder and the original gets \\Answered / $Forwarded, in a
    Celery task (best-effort; never fails the send).
    """
    from .models import EmailMessage

    if report is None:
        report = {}
    report.setdefault('refused_recipients', [])
    report.setdefault('warnings', [])

    cc_emails = cc_emails or []
    bcc_emails = bcc_emails or []
    attachments = attachments or []

    original = reply_to_message
    if original is None and reply_to_message_id:
        original = (
            EmailMessage.objects.filter(message_id=reply_to_message_id, connection=connection).first()
            or EmailMessage.objects.filter(message_id=reply_to_message_id).first()
        )
    is_reply = original is not None or reply_to_message_id is not None

    signature_html_content = None
    signature_text_content = None
    if not signature_included:
        signature = get_effective_signature(connection, is_reply=is_reply)
        if signature['applies']:
            signature_html_content = signature['signature_html'] or None
            signature_text_content = signature['signature_text'] or None

    # Append signature to plain text body
    final_body_text = body_text
    if signature_text_content and body_text:
        final_body_text = f"{body_text}\n\n--\n{signature_text_content}"

    # Wrap HTML body in proper email template with signature
    final_body_html = None
    if body_html:
        final_body_html = wrap_html_email(body_html, signature_html_content)

    # Create message body (base64 images -> inline cid parts, html_mime_part)
    inline_domain = connection.email_address.rpartition('@')[2] or None
    if final_body_html and final_body_text:
        body_part = MIMEMultipart('alternative')
        body_part.attach(MIMEText(final_body_text, 'plain', 'utf-8'))
        body_part.attach(html_mime_part(final_body_html, inline_domain))
    elif final_body_html:
        body_part = html_mime_part(final_body_html, inline_domain)
    else:
        body_part = MIMEText(final_body_text or '', 'plain', 'utf-8')

    # Wrap in 'mixed' if there are attachments, otherwise use body directly
    if attachments:
        msg = MIMEMultipart('mixed')
        msg.attach(body_part)
        for att in attachments:
            maintype, _, subtype = (att.get('content_type') or 'application/octet-stream').partition('/')
            if not subtype:
                subtype = 'octet-stream'
            filename = att.get('filename', 'file')
            mime_att = MIMEBase(maintype, subtype)
            mime_att.set_payload(att['content'])
            encoders.encode_base64(mime_att)
            # name= on Content-Type too: some Outlook versions ignore the
            # Content-Disposition filename and show ATT00001.bin.
            mime_att.set_param('name', filename)
            mime_att.add_header('Content-Disposition', 'attachment', filename=filename)
            msg.attach(mime_att)
    else:
        msg = body_part

    # Set headers
    # Default make_msgid() uses the host name, which is the container id in
    # production; spam filters score a non-resolving domain negatively.
    sender_domain = connection.email_address.rpartition('@')[2] or None
    msg['Message-ID'] = make_msgid(domain=sender_domain)
    msg['From'] = formataddr((connection.display_name or '', connection.email_address))
    msg['To'] = ', '.join(to_emails)
    if cc_emails:
        msg['Cc'] = ', '.join(cc_emails)
    msg['Subject'] = subject
    msg['Date'] = formatdate(localtime=True)

    # Handle reply threading
    thread_id = None
    if original is not None:
        msg['In-Reply-To'] = original.message_id
        msg['References'] = build_references(original)
        thread_id = original.thread_id

    if not thread_id:
        thread_id = compute_thread_id(msg['Message-ID'], '', '')

    # Send via SMTP. Bcc stays out of the headers (only the envelope).
    seen = set()
    all_recipients = (
        _dedupe_addresses(to_emails, seen)
        + _dedupe_addresses(cc_emails, seen)
        + _dedupe_addresses(bcc_emails, seen)
    )
    smtp = None
    try:
        if connection.smtp_use_ssl:
            smtp = smtplib.SMTP_SSL(connection.smtp_server, connection.smtp_port, timeout=30)
        else:
            smtp = smtplib.SMTP(connection.smtp_server, connection.smtp_port, timeout=30)
            if connection.smtp_use_tls:
                smtp.starttls()

        smtp.login(connection.username, connection.get_password())
        refused = smtp.sendmail(connection.email_address, all_recipients, msg.as_string())
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        raise
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                try:
                    smtp.close()
                except Exception:
                    pass

    if isinstance(refused, dict) and refused:
        report['refused_recipients'] = sorted(str(addr) for addr in refused)
        report['warnings'].append(
            'The server refused some recipients: ' + ', '.join(report['refused_recipients'])
        )
        logger.warning(f"SMTP refused recipients {report['refused_recipients']} for {msg['Message-ID']}")

    # The Sent copy is the delivered MIME plus Bcc (as Outlook keeps it);
    # the SMTP payload above never carries Bcc.
    sent_copy = copy.deepcopy(msg)
    if bcc_emails:
        sent_copy['Bcc'] = ', '.join(bcc_emails)
    try:
        sent_bytes = sent_copy.as_bytes()
    except Exception:
        sent_bytes = msg.as_string().encode('utf-8')

    # Store outgoing attachments so the Sent row can link to them.
    attachment_metadata = []
    for att in attachments:
        filename = att.get('filename', 'file')
        content = att.get('content', b'') or b''
        meta = {
            'filename': filename,
            'content_type': att.get('content_type', 'application/octet-stream'),
            'size': len(content),
        }
        try:
            meta['url'] = _store_outgoing_attachment(connection, filename, content)
        except Exception as e:
            logger.error(f"Failed to store sent attachment {filename}: {e}")
        attachment_metadata.append(meta)

    # Save sent message to database: exactly what was delivered.
    sent_message = None
    try:
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
            reply_to='',
            subject=subject,
            body_text=final_body_text or '',
            body_html=final_body_html or '',
            timestamp=timezone.now(),
            folder='Sent',
            is_from_business=True,
            is_read=True,
            attachments=attachment_metadata,
        )
    except Exception as e:
        # The mail is already out; never tell the user it failed (they'd resend).
        logger.error(f"Email {msg['Message-ID']} sent but saving the Sent copy failed: {e}")
        report['warnings'].append('The email was sent but could not be saved to the Sent folder in EchoDesk.')

    flag_targets = []
    other_mailbox_flags = []  # originals that live in another mailbox
    if original is not None and getattr(original, 'pk', None):
        try:
            EmailMessage.objects.filter(pk=original.pk).update(is_answered=True)
            original.is_answered = True
        except Exception as e:
            logger.warning(f"Failed to mark message {original.pk} answered: {e}")
        if original.connection_id == connection.pk:
            flag_targets.append([original.pk, '\\Answered'])
        else:
            other_mailbox_flags.append((original, '\\Answered'))
    if forward_message is not None and getattr(forward_message, 'pk', None):
        if forward_message.connection_id == connection.pk:
            flag_targets.append([forward_message.pk, '$Forwarded'])
        else:
            other_mailbox_flags.append((forward_message, '$Forwarded'))

    # A uid is only meaningful in its own mailbox: flag those originals in a
    # session of their own connection, never the sender's.
    for message, flag in other_mailbox_flags:
        try:
            from . import email_mailbox
            email_mailbox.schedule_mailbox_operation(
                message.connection, 'flag', [email_mailbox.message_item(message)], flag=flag, value=True,
            )
        except Exception as e:
            logger.warning(f"Failed to queue {flag} for message {message.pk}: {e}")

    schedule_post_send_imap(
        connection,
        None if provider_saves_sent_copy(connection) else sent_bytes,
        sent_message.pk if sent_message else None,
        flag_targets,
    )

    return msg['Message-ID'], sent_message


def _quote_mailbox(name: str) -> str:
    """Quote a (raw, modified-UTF-7) mailbox name for an IMAP command."""
    return '"' + (name or '').replace('\\', '\\\\').replace('"', '\\"') + '"'


def _find_sent_folder(imap) -> Optional[str]:
    """
    Find the Sent folder on this IMAP server and return its RAW
    (modified UTF-7) name, ready for APPEND/SELECT after quoting.

    Same choice as sync (``email_mailbox.list_mailboxes``): the RFC 6154
    \\Sent special-use folder, else the first well-known Sent name in LIST
    order. None when the server has no such folder.
    """
    from . import email_mailbox
    try:
        for entry in email_mailbox.list_mailboxes(imap) or []:
            if entry['db_name'] == email_mailbox.CANONICAL_FOLDERS['sent']:
                return entry['raw_name']
    except Exception as e:
        logger.warning(f"Error finding sent folder: {e}")
    return None


_APPENDUID_RE = re.compile(rb'APPENDUID\s+(\d+)\s+(\d+)', re.IGNORECASE)


def append_to_sent_folder(connection, message_bytes: bytes, imap=None) -> Optional[str]:
    """APPEND ``message_bytes`` (flagged \\Seen) to the server's Sent folder.

    Returns the new UID when the server answers with APPENDUID (UIDPLUS),
    else None. Raises on failure so callers can retry.
    """
    return _append_to_sent(connection, message_bytes, imap=imap)[0]


def _append_to_sent(connection, message_bytes: bytes, imap=None) -> Tuple[Optional[str], str]:
    """``append_to_sent_folder`` returning ``(uid, uidvalidity)`` from APPENDUID.

    The target is the folder sync stores as "Sent" (``resolve_raw_folder``),
    created when the server has none, so the APPENDed copy is the one sync
    sees and the uid is recorded against the right folder.
    """
    from . import email_mailbox
    own_imap = imap is None
    if own_imap:
        imap = _connect_imap(connection)
    try:
        target = email_mailbox.resolve_raw_folder(imap, email_mailbox.CANONICAL_FOLDERS['sent'], create=True)
        if not target:
            target = 'Sent'

        typ, data = imap.append(
            _quote_mailbox(target), '(\\Seen)',
            imaplib.Time2Internaldate(time.time()), message_bytes,
        )
        if typ != 'OK':
            raise imaplib.IMAP4.error(f"APPEND to {target!r} failed: {data!r}")

        for item in data or []:
            if isinstance(item, str):
                item = item.encode('utf-8', errors='replace')
            if isinstance(item, bytes):
                match = _APPENDUID_RE.search(item)
                if match:
                    return match.group(2).decode(), match.group(1).decode()
        return None, ''
    finally:
        if own_imap:
            try:
                imap.logout()
            except Exception:
                pass


def imap_add_flag(imap, message, flag: str) -> bool:
    """Best-effort ``UID STORE +FLAGS (flag)`` on ``message`` in its folder.

    ``imap`` must be a session of ``message``'s own connection. The stored
    uid is only used once its Message-ID / UIDVALIDITY check out
    (``resolve_item_uids``); otherwise the uid is searched by Message-ID.
    """
    from . import email_mailbox
    raw_folder = email_mailbox.resolve_raw_folder(imap, message.folder)
    if not raw_folder:
        return False
    ok, uidvalidity, _ = email_mailbox.select_folder(imap, raw_folder, readonly=False)
    if not ok:
        return False
    resolved = email_mailbox.resolve_item_uids(imap, [email_mailbox.message_item(message)], uidvalidity)
    if not resolved:
        return False
    typ, _ = imap.uid('STORE', resolved[0][1], '+FLAGS', f'({flag})')
    return typ == 'OK'


def perform_post_send_imap(connection, message_bytes: Optional[bytes], sent_message_id=None,
                           flag_targets=None) -> Optional[str]:
    """Server-side bookkeeping after a send, in one IMAP session.

    Flags the originals (best-effort, never raises), then APPENDs the sent
    copy when ``message_bytes`` is given (raises on failure so the Celery task
    retries) and stores the APPENDUID on the sent row.
    """
    from .models import EmailMessage

    imap = _connect_imap(connection)
    try:
        for message_pk, flag in flag_targets or []:
            try:
                # Only this connection's messages: a uid of another mailbox
                # would flag an unrelated message here.
                message = EmailMessage.objects.filter(pk=message_pk, connection=connection).first()
                if message is not None:
                    imap_add_flag(imap, message, flag)
            except Exception as e:
                logger.warning(f"Failed to set {flag} on message {message_pk}: {e}")

        uid = None
        if message_bytes is not None:
            uid, uidvalidity = _append_to_sent(connection, message_bytes, imap=imap)
            if uid and sent_message_id:
                EmailMessage.objects.filter(pk=sent_message_id).update(uid=uid, uid_validity=uidvalidity or '')
        return uid
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def schedule_post_send_imap(connection, message_bytes: Optional[bytes], sent_message_id=None,
                            flag_targets=None) -> None:
    """Queue perform_post_send_imap in Celery once the DB transaction commits.

    Falls back to running it inline when the broker is unavailable. Never
    raises: the mail has already been delivered.
    """
    if message_bytes is None and not flag_targets:
        return
    from django.db import connection as db_connection, transaction

    schema_name = getattr(db_connection, 'schema_name', None)
    eml_b64 = base64.b64encode(message_bytes).decode('ascii') if message_bytes is not None else None
    targets = [list(t) for t in (flag_targets or [])]

    def _dispatch():
        try:
            from .tasks import email_post_send_task
            email_post_send_task.delay(schema_name, connection.pk, sent_message_id, eml_b64, targets)
            return
        except Exception as e:
            logger.warning(f"Could not queue post-send IMAP task, running inline: {e}")
        try:
            perform_post_send_imap(connection, message_bytes, sent_message_id, targets)
        except Exception as e:
            logger.error(f"Post-send IMAP (Sent APPEND) failed for {connection.email_address}: {e}")

    try:
        transaction.on_commit(_dispatch)
    except Exception as e:
        logger.error(f"Failed to schedule post-send IMAP work: {e}")


SYNC_HEADER_BATCH = 200
SYNC_BODY_BATCH = 25


def _sync_window_start(connection):
    """Oldest timestamp inside the synced date window (None = all history)."""
    if connection.sync_days_back and connection.sync_days_back > 0:
        return timezone.now() - timedelta(days=connection.sync_days_back)
    return None


def _flag_changes(row: Dict[str, Any], flags: set, held: bool = False) -> Dict[str, Any]:
    """DB field updates that bring an existing row in line with server FLAGS.

    ``held``: the row's read/star state was just changed in EchoDesk and the
    server STORE is still queued, so only \\Answered is taken from the server.
    """
    changes = {}
    if held:
        if '\\answered' in flags and not row['is_answered']:
            changes['is_answered'] = True
        return changes
    seen = '\\seen' in flags
    if row['is_read'] != seen:
        changes['is_read'] = seen
        changes['is_read_by_staff'] = seen
        changes['read_by_staff_at'] = timezone.now() if seen else None
    elif seen and not row['is_read_by_staff']:
        # Read in Outlook/webmail: the unified unread badge should agree.
        changes['is_read_by_staff'] = True
        changes['read_by_staff_at'] = timezone.now()
    starred = '\\flagged' in flags
    if row['is_starred'] != starred:
        changes['is_starred'] = starred
    # \\Answered is only ever added: EchoDesk sets is_answered on send before
    # the server STORE runs (or when the original's uid can't be found), and
    # a later sync must not quietly undo that.
    if '\\answered' in flags and not row['is_answered']:
        changes['is_answered'] = True
    return changes


def _build_synced_message(connection, raw_email: bytes, message_id: str, db_folder_name: str,
                          uid: str, uidvalidity: str, flags: set):
    """EmailMessage (unsaved) for one RFC822 message fetched by sync."""
    from .models import EmailMessage
    from .email_mailbox import normalize_message_id

    email_message = email.message_from_bytes(raw_email)

    from_name, from_email_addr = parseaddr(email_message.get('From', ''))
    from_name = decode_mime_header(from_name)

    to_emails = parse_address_list(email_message.get('To', ''))
    cc_emails = parse_address_list(email_message.get('Cc', ''))
    reply_to = parseaddr(email_message.get('Reply-To', '') or '')[1]
    if reply_to and ('@' not in reply_to or len(reply_to) > 254):
        reply_to = ''

    try:
        timestamp = parsedate_to_datetime(email_message.get('Date'))
        if timestamp.tzinfo is None:
            timestamp = timezone.make_aware(timestamp)
    except Exception:
        timestamp = timezone.now()

    body_text, body_html = extract_body(email_message)
    message_attachments = extract_attachments(email_message, connection)

    references = ' '.join((email_message.get('References', '') or '').split())
    in_reply_to = normalize_message_id(email_message.get('In-Reply-To', ''))
    thread_id = compute_thread_id(message_id, in_reply_to, references)

    is_read = '\\seen' in flags
    return EmailMessage(
        connection=connection,
        message_id=message_id,
        thread_id=thread_id,
        in_reply_to=in_reply_to[:500],
        references=references,
        from_email=from_email_addr,
        from_name=from_name[:200],
        to_emails=to_emails,
        cc_emails=cc_emails,
        reply_to=reply_to if reply_to.lower() != (from_email_addr or '').lower() else '',
        subject=decode_mime_header(email_message.get('Subject', ''))[:1000],
        body_text=body_text,
        body_html=body_html,
        attachments=message_attachments,
        timestamp=timestamp,
        folder=db_folder_name,
        uid=uid,
        uid_validity=uidvalidity,
        is_from_business=(from_email_addr or '').lower() == connection.email_address.lower(),
        is_read=is_read,
        is_read_by_staff=is_read,
        is_starred='\\flagged' in flags,
        is_answered='\\answered' in flags,
    )


def _sync_folder(imap, connection, imap_folder_name: str, db_folder_name: str, max_messages: int,
                 state: Optional[Dict[str, Any]] = None) -> int:
    """
    Sync messages from a specific IMAP folder.

    Cheap path first: UID SEARCH, then one UID FETCH (UID FLAGS Message-ID
    header) for the synced window. Only messages not yet stored are
    downloaded in full (BODY.PEEK[], so nothing is marked read). Existing
    rows get their uid/UIDVALIDITY and \\Seen/\\Flagged/\\Answered refreshed.

    ``state`` (optional) is filled for ``sync_imap_messages``' reconciliation:
    ok, all_uids, uidvalidity, legacy_uids_consistent and move candidates
    (rows stored under another folder that is synced in the same run).

    Args:
        imap: IMAP connection object
        connection: EmailConnection model instance
        imap_folder_name: Raw folder name for IMAP operations (may be UTF-7 encoded)
        db_folder_name: Decoded/canonical folder name stored in the database
        max_messages: Maximum messages to sync per folder

    Returns:
        Number of new messages synced from this folder
    """
    from django.db import IntegrityError, transaction
    from .models import EmailMessage
    from . import email_mailbox as mailbox

    if state is None:
        state = {}
    run_folders = state.get('run_folders') or set()
    state.update({
        'ok': False, 'db_folder': db_folder_name, 'raw_folder': imap_folder_name,
        'all_uids': set(), 'uidvalidity': '', 'exists': 0,
        'legacy_uids_consistent': False, 'candidates': [],
    })

    try:
        ok, uidvalidity, exists = mailbox.select_folder(imap, imap_folder_name, readonly=True)
        if not ok:
            logger.warning(f"[EMAIL_SYNC] Failed to select folder '{db_folder_name}'")
            return 0
        logger.info(f"[EMAIL_SYNC] Selected folder '{db_folder_name}' - {exists} total messages on server")
    except (imaplib.IMAP4.abort, OSError):
        raise
    except Exception as e:
        logger.warning(f"[EMAIL_SYNC] Error selecting folder '{db_folder_name}': {e}")
        return 0

    result, data = imap.uid('SEARCH', None, 'ALL')
    if result != 'OK':
        logger.warning(f"[EMAIL_SYNC] UID SEARCH ALL failed in '{db_folder_name}': {result}")
        return 0
    all_uids = sorted({int(u) for u in (data[0].split() if data and data[0] else []) if u.isdigit()})

    window_start = _sync_window_start(connection)
    if window_start is not None:
        since_date = window_start.strftime('%d-%b-%Y')
        result, data = imap.uid('SEARCH', None, f'(SINCE {since_date})')
        if result != 'OK':
            logger.warning(f"[EMAIL_SYNC] UID SEARCH SINCE failed in '{db_folder_name}': {result}")
            return 0
        window = sorted({int(u) for u in (data[0].split() if data and data[0] else []) if u.isdigit()})
    else:
        window = list(all_uids)
    logger.info(f"[EMAIL_SYNC] Found {len(window)} messages in '{db_folder_name}' matching search criteria")
    if len(window) > max_messages:
        window = window[-max_messages:]

    # Phase 1: UID + FLAGS + Message-ID for the window (no bodies).
    headers = {}
    for chunk in mailbox._chunks(window, SYNC_HEADER_BATCH):
        result, data = imap.uid(
            'FETCH', ','.join(str(u) for u in chunk),
            f'(UID FLAGS BODY.PEEK[HEADER.FIELDS ({mailbox.SYNC_HEADER_FIELDS})])',
        )
        if result != 'OK':
            logger.warning(f"[EMAIL_SYNC] Header FETCH failed in '{db_folder_name}': {result}")
            return 0
        for entry in mailbox.parse_fetch_response(data):
            raw_message_id = mailbox._raw_header_message_id(entry['body'])
            message_id = mailbox.normalize_message_id(raw_message_id)
            if not message_id:
                raw_message_id = ''
                message_id = mailbox.synthetic_message_id(
                    connection.pk, imap_folder_name, uidvalidity, entry['uid'], entry['body'],
                )
            headers[entry['uid']] = {
                'message_id': message_id[:500],
                # Rows stored by the pre-UID sync kept the raw header value
                # (folding whitespace included); look those up too.
                'raw_message_id': raw_message_id[:500] if raw_message_id[:500] != message_id[:500] else '',
                'flags': entry['flags'],
            }

    # Phase 2: existing rows of THIS mailbox (dedupe is per connection).
    existing = {}
    message_ids = [h['message_id'] for h in headers.values()]
    message_ids += [h['raw_message_id'] for h in headers.values() if h['raw_message_id']]
    for chunk in mailbox._chunks(message_ids, 500):
        for row in EmailMessage.objects.filter(connection=connection, message_id__in=chunk).values(
            'id', 'message_id', 'folder', 'uid', 'uid_validity',
            'is_read', 'is_starred', 'is_answered', 'is_read_by_staff', 'is_deleted',
        ):
            existing[row['message_id']] = row
    # Flags changed in EchoDesk whose server STORE hasn't run yet: the
    # server's (older) flags must not overwrite them.
    held = mailbox.held_flag_ids(row['id'] for row in existing.values())

    new_uids = []
    updates = []
    candidates = []
    legacy_checked = 0
    legacy_mismatch = False
    seen_new_ids = set()
    for uid, header in headers.items():
        row = existing.get(header['message_id'])
        renamed = {}
        if row is None and header['raw_message_id']:
            row = existing.get(header['raw_message_id'])
            if row is not None:
                # Legacy row keyed by the un-normalised header: adopt it
                # (and normalise its id) instead of inserting a duplicate.
                renamed = {'message_id': header['message_id']}
        if row is None:
            if header['message_id'] not in seen_new_ids:
                seen_new_ids.add(header['message_id'])
                new_uids.append(uid)
            continue
        if row['is_deleted']:
            if renamed:
                updates.append((row['id'], renamed))
            continue
        if row['folder'] == db_folder_name:
            if row['uid'] and not row['uid_validity']:
                legacy_checked += 1
                if row['uid'] != uid:
                    legacy_mismatch = True
            changes = _flag_changes(row, header['flags'], held=row['id'] in held)
            changes.update(renamed)
            if row['uid'] != uid or row['uid_validity'] != uidvalidity:
                changes['uid'] = uid
                changes['uid_validity'] = uidvalidity
            if changes:
                updates.append((row['id'], changes))
        elif row['folder'] not in run_folders:
            # Stored under a folder this run doesn't sync (renamed, or an old
            # non-canonical name): it lives here now.
            changes = _flag_changes(row, header['flags'], held=row['id'] in held)
            changes.update(renamed)
            changes.update(folder=db_folder_name, uid=uid, uid_validity=uidvalidity)
            updates.append((row['id'], changes))
        else:
            # Same Message-ID in two synced folders: only re-folder if the
            # old folder no longer has it (decided after every folder ran).
            if renamed:
                updates.append((row['id'], renamed))
            changes = _flag_changes(row, header['flags'], held=row['id'] in held)
            changes.update(folder=db_folder_name, uid=uid, uid_validity=uidvalidity)
            candidates.append({'id': row['id'], 'old_folder': row['folder'], 'changes': changes})

    for pk, changes in updates:
        if 'message_id' in changes:
            try:
                with transaction.atomic():
                    EmailMessage.objects.filter(pk=pk).update(**changes)
                continue
            except IntegrityError:
                # Normalised id already taken by another row: keep the old one.
                changes = {k: v for k, v in changes.items() if k != 'message_id'}
                if not changes:
                    continue
        EmailMessage.objects.filter(pk=pk).update(**changes)
    if updates:
        logger.info(f"[EMAIL_SYNC] Refreshed {len(updates)} existing messages in '{db_folder_name}'")

    # Phase 3: download only the new messages.
    messages_to_create = []
    for chunk in mailbox._chunks(new_uids, SYNC_BODY_BATCH):
        try:
            result, data = imap.uid('FETCH', ','.join(chunk), '(UID FLAGS BODY.PEEK[])')
        except (imaplib.IMAP4.abort, OSError):
            raise
        except Exception as e:
            logger.warning(f"[EMAIL_SYNC] Body FETCH failed in '{db_folder_name}': {e}")
            continue
        if result != 'OK':
            continue
        for entry in mailbox.parse_fetch_response(data):
            header = headers.get(entry['uid'])
            if header is None or not entry['body']:
                continue
            try:
                messages_to_create.append(_build_synced_message(
                    connection, entry['body'], header['message_id'], db_folder_name,
                    entry['uid'], uidvalidity, entry['flags'] or header['flags'],
                ))
            except Exception as e:
                logger.warning(f"[EMAIL_SYNC] Failed to process UID {entry['uid']} in '{db_folder_name}': {e}")

    if messages_to_create:
        EmailMessage.objects.bulk_create(messages_to_create, ignore_conflicts=True)
        logger.info(f"[EMAIL_SYNC] Bulk created {len(messages_to_create)} messages")

    state.update({
        'ok': True,
        'all_uids': set(all_uids),
        'uidvalidity': uidvalidity,
        'exists': exists,
        'legacy_uids_consistent': legacy_checked > 0 and not legacy_mismatch,
        'candidates': candidates,
    })
    logger.info(f"[EMAIL_SYNC] Folder '{db_folder_name}' sync complete: {len(messages_to_create)} new messages created")
    return len(messages_to_create)


def _reconcile_synced_folders(connection, states: List[Dict[str, Any]]) -> Dict[str, int]:
    """Apply server-side deletions/moves inside the synced window.

    For each folder that synced cleanly, stored rows whose uid (recorded under
    the folder's current UIDVALIDITY) is no longer on the server are either
    re-foldered (the same Message-ID was seen in another synced folder) or
    marked is_deleted. Skipped when UIDVALIDITY is unknown/changed for a row,
    or when the server's UID list disagrees with its EXISTS count.
    """
    from django.db.models import Q
    from .models import EmailMessage

    candidates = {}
    for state in states:
        for candidate in state.get('candidates') or []:
            candidates.setdefault(candidate['id'], candidate)

    window_start = _sync_window_start(connection)
    stats = {'deleted': 0, 'moved': 0}
    now = timezone.now()
    for state in states:
        if not state.get('ok'):
            continue
        if len(state['all_uids']) != state.get('exists', len(state['all_uids'])):
            logger.warning(
                f"[EMAIL_SYNC] '{state['db_folder']}': UID list ({len(state['all_uids'])}) != EXISTS "
                f"({state.get('exists')}); skipping deletion reconciliation"
            )
            continue
        trusted = Q(pk__in=[])
        if state['uidvalidity']:
            trusted |= Q(uid_validity=state['uidvalidity'])
        if state['legacy_uids_consistent']:
            trusted |= Q(uid_validity='')
        rows = EmailMessage.objects.filter(
            connection=connection, folder=state['db_folder'], is_deleted=False,
        ).exclude(uid='').filter(trusted)
        if window_start is not None:
            rows = rows.filter(timestamp__gte=window_start)

        gone_ids = []
        for row in rows.values('id', 'uid'):
            try:
                uid = int(row['uid'])
            except (TypeError, ValueError):
                continue
            if uid in state['all_uids']:
                continue
            candidate = candidates.pop(row['id'], None)
            if candidate is not None:
                EmailMessage.objects.filter(pk=row['id']).update(**candidate['changes'])
                stats['moved'] += 1
            else:
                gone_ids.append(row['id'])
        if gone_ids:
            stats['deleted'] += EmailMessage.objects.filter(pk__in=gone_ids).update(
                is_deleted=True, deleted_at=now,
            )
    if stats['deleted'] or stats['moved']:
        logger.info(
            f"[EMAIL_SYNC] Reconciled {connection.email_address}: {stats['moved']} moved, "
            f"{stats['deleted']} removed on the server"
        )
    return stats


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
        from users.notification_texts import T
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
                title=T('email_sync.disabled.title', email=connection.email_address),
                # The server's error text stays as reported.
                message=T(
                    'email_sync.disabled.message',
                    email=connection.email_address, error=error_message[:200],
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


SYNC_LOCK_TIMEOUT = 30 * 60

_AUTH_ERROR_MARKERS = (
    'authenticationfailed', 'authentication failed', 'invalid credentials',
    'login failed', 'logon failure', 'incorrect password', 'invalid password',
    'authenticate failed', 'auth failed', '[auth]', 'login incorrect',
    'username or password',
)


def _is_imap_auth_error(error: Exception) -> bool:
    """True only for credential problems (the user must fix the password)."""
    if not isinstance(error, imaplib.IMAP4.error) or isinstance(error, imaplib.IMAP4.abort):
        return False
    text = str(error).lower()
    return any(marker in text for marker in _AUTH_ERROR_MARKERS)


def _sync_lock_key(connection) -> str:
    from django.db import connection as db_connection
    schema = getattr(db_connection, 'schema_name', '') or 'public'
    return f'email-sync-lock:{schema}:{connection.pk}'


def _acquire_sync_lock(key: str) -> bool:
    """cache.add-based lock. A cache outage never blocks syncing."""
    from django.core.cache import cache
    try:
        acquired = cache.add(key, timezone.now().isoformat(), SYNC_LOCK_TIMEOUT)
    except Exception as e:
        logger.warning(f"[EMAIL_SYNC] Sync lock unavailable ({e}); syncing without it")
        return True
    # django-redis with IGNORE_EXCEPTIONS returns None on a Redis error.
    return acquired is not False


def _release_sync_lock(key: str) -> None:
    from django.core.cache import cache
    try:
        cache.delete(key)
    except Exception:
        pass


def _record_sync_failure(connection, error: Exception) -> None:
    """Count a failed run; auto-disable past SYNC_FAILURE_THRESHOLD."""
    connection.sync_failure_count = (connection.sync_failure_count or 0) + 1
    failure_count = connection.sync_failure_count

    if failure_count >= SYNC_FAILURE_THRESHOLD:
        logger.warning(
            f"[EMAIL_SYNC] {connection.email_address} hit "
            f"{failure_count} consecutive errors; auto-disabling. "
            f"Last error: {error}"
        )
        _auto_disable_email_connection(
            connection,
            f"Disabled after {failure_count} consecutive failures. Last error: {error}",
        )
    else:
        logger.warning(
            f"Email sync error for {connection.email_address} "
            f"({failure_count}/{SYNC_FAILURE_THRESHOLD}): {error}"
        )
        connection.last_sync_error = str(error)
        connection.save(update_fields=['last_sync_error', 'sync_failure_count'])


def sync_imap_messages(connection, max_messages: int = 500) -> int:
    """
    Sync messages from IMAP server for a given connection.

    Syncs every selectable folder except Drafts and Gmail's All Mail. Special
    folders are stored under canonical names (C4): \\Sent -> "Sent",
    \\Trash -> "Trash", \\Junk -> "Junk", \\Archive -> "Archive"; custom
    folders keep their decoded name. After all folders ran, server-side
    deletions/moves inside the synced window are reconciled.

    A per-connection cache lock keeps overlapping runs (beat + manual sync)
    from racing. If the IMAP connection drops mid-sync it reconnects and
    retries the folder (up to 2 retries). Only authentication failures
    disable the connection at once; other errors count toward
    SYNC_FAILURE_THRESHOLD.

    Args:
        connection: EmailConnection model instance
        max_messages: Maximum number of messages to fetch per sync per folder

    Returns:
        Number of new messages synced
    """
    lock_key = _sync_lock_key(connection)
    if not _acquire_sync_lock(lock_key):
        logger.info(f"[EMAIL_SYNC] Sync already running for {connection.email_address}; skipping")
        return 0
    try:
        return _sync_imap_messages_locked(connection, max_messages)
    finally:
        _release_sync_lock(lock_key)


def _sync_imap_messages_locked(connection, max_messages: int) -> int:
    from . import email_mailbox as mailbox

    try:
        # Connect to IMAP
        imap = _connect_imap(connection)

        total_new_count = 0

        mailboxes = mailbox.list_mailboxes(imap)
        if mailboxes is not None:
            folders_to_sync = []
            skipped_folders = []
            for entry in mailboxes:
                if entry['flags'] & mailbox.SYNC_SKIP_FLAGS or entry['name'].lower() in mailbox.SYNC_SKIP_NAMES \
                        or entry['role'] == 'drafts':
                    skipped_folders.append(entry['name'])
                else:
                    folders_to_sync.append((entry['raw_name'], entry['db_name']))

            # Log folder discovery details
            logger.info(f"[EMAIL_SYNC] ===== FOLDER DISCOVERY for {connection.email_address} =====")
            logger.info(f"[EMAIL_SYNC] All folders on server ({len(mailboxes)}): {[e['name'] for e in mailboxes]}")
            logger.info(f"[EMAIL_SYNC] Folders to sync ({len(folders_to_sync)}): {[f[1] for f in folders_to_sync]}")
            logger.info(f"[EMAIL_SYNC] Skipped folders ({len(skipped_folders)}): {skipped_folders}")
            logger.info(f"[EMAIL_SYNC] sync_days_back setting: {connection.sync_days_back} (0=all history)")

            run_folders = {db_name for _, db_name in folders_to_sync}
            states = []

            # Sync each folder with automatic reconnection on connection drop
            for raw_folder_name, display_folder_name in folders_to_sync:
                max_retries = 2
                for attempt in range(max_retries + 1):
                    state = {'run_folders': run_folders}
                    try:
                        folder_count = _sync_folder(
                            imap, connection, raw_folder_name, display_folder_name, max_messages, state,
                        )
                        states.append(state)
                        total_new_count += folder_count
                        if folder_count > 0:
                            logger.info(f"Synced {folder_count} emails from {display_folder_name}")
                        break  # Success, move to next folder
                    except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, ConnectionError, BrokenPipeError) as e:
                        error_msg = str(e).lower()
                        is_connection_error = isinstance(e, imaplib.IMAP4.abort) or any(keyword in error_msg for keyword in [
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
                _reconcile_synced_folders(connection, states)
            except Exception as e:
                logger.error(f"[EMAIL_SYNC] Reconciliation failed for {connection.email_address}: {e}")

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

    except Exception as e:
        if _is_imap_auth_error(e):
            # Wrong credentials: disable immediately. The user must fix the
            # password and click Reactivate in the UI to resume sync.
            logger.error(f"Email sync authentication failed for {connection.email_address}: {e}")
            _auto_disable_email_connection(connection, str(e))
        else:
            # Transient SSL/socket errors, server BAD/NO replies, timeouts,
            # host resolution, code bugs: retry next cycle, disable only after
            # SYNC_FAILURE_THRESHOLD consecutive failures.
            _record_sync_failure(connection, e)
        return 0


def delete_emails_from_imap(connection, message_ids: List[str]) -> int:
    """
    Move emails to the server's Trash folder (kept for back-compat).

    Uses the UID-only mailbox helpers: never a bare EXPUNGE, and it works in
    whichever folder each message is stored in, not just INBOX.

    Args:
        connection: EmailConnection model instance
        message_ids: List of Message-ID headers to delete

    Returns:
        Number of emails moved to Trash
    """
    from .models import EmailMessage
    from . import email_mailbox as mailbox

    if not message_ids:
        return 0
    rows = EmailMessage.objects.filter(connection=connection, message_id__in=message_ids).exclude(folder='Trash')
    items = [mailbox.message_item(row) for row in rows]
    summary = mailbox.perform_mailbox_operation(connection, 'move', items, target_folder='Trash')
    logger.info(f"Moved {summary['done']} emails to Trash for {connection.email_address}")
    return summary['done']


def move_email_to_folder(connection, message_id: str, source_folder: str, target_folder: str,
                         result: Optional[Dict[str, Any]] = None) -> bool:
    """
    Move an email from one IMAP folder to another.

    Folder names may be DB/canonical names ("Sent", "Trash") or decoded IMAP
    names, including non-ASCII and space-containing ones: they are resolved
    to the raw modified-UTF-7 name and quoted. Uses UID MOVE when the server
    supports it, else UID COPY + UID STORE \\Deleted + UID EXPUNGE <uid>
    (never a bare EXPUNGE). The new UID (COPYUID) is stored on the row.

    Args:
        connection: EmailConnection model instance
        message_id: Message-ID header of the email to move
        source_folder: Current folder of the email
        target_folder: Destination folder
        result: Optional dict; on success ``result['db_folder']`` is the DB
            folder name of the folder the server moved it to (as sync stores it)

    Returns:
        True if successful, False otherwise
    """
    from .models import EmailMessage
    from . import email_mailbox as mailbox

    imap = None
    try:
        row = EmailMessage.objects.filter(connection=connection, message_id=message_id).first()
        item = mailbox.message_item(row) if row is not None else {
            'id': None, 'uid': '', 'uid_validity': '', 'message_id': message_id,
        }
        if row is not None and row.folder != source_folder:
            # The client's idea of the folder wins; the stored uid belongs
            # to another folder.
            item.update(uid='', uid_validity='')
        item['folder'] = source_folder

        imap = _connect_imap(connection)
        mailboxes = mailbox.list_mailboxes(imap) or []
        source_raw = mailbox.resolve_raw_folder(imap, source_folder, mailboxes)
        target_raw = mailbox.resolve_raw_folder(
            imap, target_folder, mailboxes,
            create=mailbox.role_for_db_folder(target_folder) in ('trash', 'archive', 'junk'),
        )
        if not source_raw or not target_raw:
            logger.warning(f"Move: folder not found (source={source_folder!r}, target={target_folder!r})")
            return False

        ok, uidvalidity, _ = mailbox.select_folder(imap, source_raw, readonly=False)
        if not ok:
            raise Exception(f"Failed to select source folder {source_folder}")

        resolved = mailbox.resolve_item_uids(imap, [item], uidvalidity)
        if not resolved:
            logger.warning(f"Message {message_id} not found in {source_folder}")
            return False
        uid = resolved[0][1]

        ok, target_validity, mapping = mailbox.uid_move(imap, [uid], target_raw)
        if not ok:
            raise Exception(f"Failed to move message to {target_folder}")

        if row is not None:
            new_uid = mapping.get(uid, '')
            EmailMessage.objects.filter(pk=row.pk).update(
                uid=new_uid, uid_validity=target_validity if new_uid else '',
            )
        if result is not None:
            entry = next((m for m in mailboxes if m['raw_name'] == target_raw), None)
            # Not listed = just CREATEd for its role (Trash/Junk/Archive).
            result['db_folder'] = entry['db_name'] if entry else mailbox.canonical_db_folder(target_folder)

        logger.info(f"Moved email {message_id} from {source_folder} to {target_folder}")
        return True

    except Exception as e:
        logger.error(f"IMAP move error: {e}")
        return False
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass


def get_available_folders(connection) -> List[Dict[str, Any]]:
    """
    Get list of available IMAP folders for a connection.

    Returns every selectable folder (Sent/Trash/Junk/Drafts included) with
    its role and the DB folder name messages are stored under.

    Returns:
        List of folder info dicts: name, display_name, db_name, raw_name, role
    """
    from . import email_mailbox as mailbox

    try:
        imap = _connect_imap(connection)
        try:
            entries = mailbox.list_mailboxes(imap) or []
        finally:
            try:
                imap.logout()
            except Exception:
                pass
        return [
            {
                'name': entry['name'],
                'display_name': entry['name'].replace('[Gmail]/', '').replace('INBOX/', ''),
                'db_name': entry['db_name'],
                'raw_name': entry['raw_name'],
                'role': entry['role'],
            }
            for entry in entries
        ]

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
            raw_folder_name = parse_imap_list_entry(folder_data)  # Keep raw for IMAP
            if not raw_folder_name:
                continue
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
