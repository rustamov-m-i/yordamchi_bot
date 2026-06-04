"""Voice transcription.

Provider chain (first one with a configured API key wins, with the next as
fallback on failure):

  1. **Aisha AI** (primary)  — Uzbek-native, pay-per-minute (~425 UZS/min, 2026).
     Data stays in Uzbekistan. 90%+ claimed accuracy. Speaker diarization
     available. Endpoint is `POST /api/v1/stt/post/` (sync — best for short
     Telegram voice messages; v2 is async for long-form audio).
       Auth header: `X-Api-Key: <key>`
       Multipart fields: audio, language (uz/ru/en)
       Response: {"transcript": "...", "status": "SUCCESS", "duration": ...}

  2. **Muxlisa.uz** (legacy secondary) — kept on the chain for the transition
     period. Remove MUXLISA_API_KEY from .env to fully decommission. Uses a
     pinned cert because its endpoint is signed by a private CA.
       Auth header: `x-api-key: <key>`
       Multipart fields: audio, language
       Response: {"text": "..."}

  3. **OpenAI Whisper** (final fallback) — auto-detect + Uzbek primer. Used
     only if both Uzbek-native providers fail.
"""

import asyncio
import hashlib
import io
import logging
import socket
import ssl
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import certifi
import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

import config
import database

logger = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 5 * 1024 * 1024  # 5 MB hard limit
AISHA_TIMEOUT = 45.0
MUXLISA_TIMEOUT = 45.0
WHISPER_TIMEOUT = 45.0

# Heuristic silence cutoff in bytes. An Ogg/Opus voice message of <2KB is
# almost always silence/click — Telegram's minimum recordable audio is
# ~3KB. Below this we skip the (paid) STT round-trip entirely.
_SILENCE_BYTES_THRESHOLD = 2 * 1024

def _build_ca_bundle() -> str:
    """Verify target for STT requests.

    The Uzbek STT hosts (back.aisha.group, service.muxlisa.uz) sit behind a
    corporate SSL-inspection proxy that re-signs TLS with a private root which
    is NOT in certifi. With plain certifi, httpx fails every Aisha/Muxlisa call
    with CERTIFICATE_VERIFY_FAILED ("self-signed certificate in chain") and the
    bot silently falls back to Whisper — which transcribes Uzbek poorly, so
    voice "isn't understood". Fix WITHOUT disabling verification: trust the
    OS-installed roots too by merging certifi with the system keychain (macOS),
    written to a file httpx can use as `verify`. Falls back to certifi on any
    error or unsupported platform (Linux already exposes corporate roots via the
    system bundle when present).
    """
    try:
        bundle = certifi.contents()
        if sys.platform == "darwin":
            extra = []
            for kc in ("/Library/Keychains/System.keychain",
                       "/System/Library/Keychains/SystemRootCertificates.keychain"):
                try:
                    out = subprocess.run(
                        ["security", "find-certificate", "-a", "-p", kc],
                        capture_output=True, text=True, timeout=15,
                    )
                    if out.returncode == 0 and "BEGIN CERTIFICATE" in out.stdout:
                        extra.append(out.stdout)
                except Exception:
                    pass
            if extra:
                bundle = bundle + "\n" + "\n".join(extra)
        out_path = Path(config.DATABASE_PATH).parent / "ca_bundle.pem"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(bundle)
        logger.info("CA bundle: %d certs (%s)", bundle.count("BEGIN CERTIFICATE"), out_path)
        return str(out_path)
    except Exception:
        logger.exception("CA bundle build failed — using certifi only")
        return certifi.where()


_CA_BUNDLE = _build_ca_bundle()

# Muxlisa's TLS endpoint is signed by a private CA ("bBakh") absent from every public
# trust store, so verification against certifi always fails. We pin the leaf cert
# instead: fetched fresh on bot startup, saved to data/muxlisa_pin.pem, and used as
# the verify bundle for Muxlisa requests only. Auto-refresh handles the ~90-day
# cert rotation; if the refresh ever fails, the Whisper fallback still covers voice.
_MUXLISA_PIN_PATH = Path(config.DATABASE_PATH).parent / "muxlisa_pin.pem"


def refresh_muxlisa_pin() -> Optional[str]:
    """Fetch service.muxlisa.uz's served chain and save it as the verify bundle.
    Returns the pin file path on success, None on failure (caller falls back to certifi).
    """
    host = urlparse(config.MUXLISA_STT_URL).hostname or "service.muxlisa.uz"
    try:
        if not hasattr(ssl.SSLSocket, "get_unverified_chain"):
            # Python <3.13: no chain API. A leaf-only pin breaks verification
            # (OpenSSL needs the issuer too), so skip pinning and let
            # _muxlisa_verify() fall back to certifi. This works as long as the
            # server uses a public CA (currently Let's Encrypt).
            logger.info("Muxlisa pin skipped (Python <3.13); using certifi")
            return None
        ctx = ssl._create_unverified_context()
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                chain = tls.get_unverified_chain()
        if not chain:
            logger.warning("Muxlisa pin refresh: empty chain from %s", host)
            return None
        _MUXLISA_PIN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_MUXLISA_PIN_PATH, "w") as f:
            for cert in chain:
                der = cert if isinstance(cert, (bytes, bytearray)) else cert.public_bytes(ssl.Purpose.CLIENT_AUTH)
                f.write(ssl.DER_cert_to_PEM_cert(der))
        logger.info("Muxlisa cert pinned: %s (chain len=%d)", _MUXLISA_PIN_PATH, len(chain))
        return str(_MUXLISA_PIN_PATH)
    except Exception:
        logger.exception("Muxlisa pin refresh failed — Whisper fallback will be used")
        return None


def _muxlisa_verify() -> str:
    """Verify target for Muxlisa: pinned leaf if present, else certifi (will fail
    on private-CA certs, but at least is well-defined)."""
    if _MUXLISA_PIN_PATH.exists():
        return str(_MUXLISA_PIN_PATH)
    return _CA_BUNDLE


# Refresh the pin on import so the first request after startup uses a current cert.
refresh_muxlisa_pin()

_openai_client = AsyncOpenAI(
    api_key=config.OPENAI_API_KEY,
    timeout=WHISPER_TIMEOUT,
    max_retries=2,
    http_client=httpx.AsyncClient(verify=_CA_BUNDLE, timeout=WHISPER_TIMEOUT),
)

# Uzbek primer for Whisper fallback only — biases auto-detect toward Uzbek vs Turkish/Azeri.
_UZBEK_PRIMER = (
    "Bu o'zbek tilida yozilgan audio. Salom, hisobot, uchrashuv, Toshkent, "
    "Agrobank, ertaga, vazifa, deadline, prioritet, marketing, byudjet."
)

# Post-correction config — every STT provider (Aisha, Muxlisa, Whisper) misses
# proper nouns, banking terms, and Russian/English loanwords occasionally. We fix
# these with a cheap Claude pass after the STT response. If correction fails for
# any reason we return the original transcript unchanged — voice messages must
# never be lost to this enhancement.
_CORRECTION_MODEL = "claude-haiku-4-5"
_CORRECTION_TIMEOUT = 15.0
_CORRECTION_MIN_LEN = 5  # skip for "ha", "yo'q", etc.

_FIXED_GLOSSARY = [
    # Workplace nouns Muxlisa often mangles
    "Agrobank", "Yordamchi", "Telegram", "Toshkent",
    # English business loanwords frequent in Uzbek speech
    "deadline", "meeting", "call", "stand-up", "follow-up", "report",
    "presentation", "demo", "feedback", "KPI", "OKR",
    # Uzbek business terms often misheard
    "kredit", "byudjet", "marketing", "biznes", "menejer", "prioritet",
    "loyiha", "vazifa", "uchrashuv", "hisobot", "muhokama",
    # Russian loanwords common in mixed speech
    "встреча", "проект", "задача", "сегодня", "завтра", "отчёт",
]

_anthropic_client = AsyncAnthropic(
    api_key=config.ANTHROPIC_API_KEY,
    timeout=_CORRECTION_TIMEOUT,
    max_retries=1,
)


async def _correct_transcript(text: str) -> str:
    """Post-correct an STT transcript using Claude + contacts/glossary.
    Works the same for any provider (Aisha, Muxlisa, Whisper).

    Returns the original text unchanged on any failure, so a broken correction
    pass never causes a voice message to be lost.
    """
    if not text or len(text.strip()) < _CORRECTION_MIN_LEN:
        return text

    try:
        contacts = await database.list_contacts()
        names = [c["name"] for c in contacts if c.get("name")]
        names_block = ", ".join(names[:40]) if names else "(yo'q)"
        glossary_block = ", ".join(_FIXED_GLOSSARY)

        prompt = (
            "Sen o'zbek tilidagi STT (speech-to-text) chiqishini post-korrektor'sisan.\n"
            "STT provayderi ayrim ismlarni, bank atamalarini va rus/ingliz so'zlarini xato\n"
            "eshitishi mumkin, hamda raqam/vaqtlarni so'z bilan yozadi.\n\n"
            "VAZIFA:\n"
            "1) Glossary bilan mos keladigan ANIQ xato eshitilgan so'zlarni tuzating.\n"
            "2) RAQAMLARNI SO'ZDAN SONGA o'tkazing:\n"
            "   • Soat vaqtlari → HH:MM formatiga: «soat o'n bir yarim» → «soat 11:30»,\n"
            "     «soat ikkida» → «soat 14:00» (ish vaqti — kunduzi PM ko'rinishida),\n"
            "     «soat to'qqiz yarim» → «soat 09:30», «yarim soatdan keyin» → «30 daqiqadan keyin»\n"
            "   • Davomiylik → raqam + birlik: «o'n daqiqa» → «10 daqiqa»,\n"
            "     «uch soat» → «3 soat», «ikki kun» → «2 kun», «bir hafta» → «1 hafta»\n"
            "   • Sanalar → DD-oy yoki DD-MM ga: «yigirma ikkinchi may» → «22-may»,\n"
            "     «o'ttizinchi sentabr» → «30-sentyabr», «to'qqizinchi» → «9-» (kontekstga qarab)\n"
            "   • Sonlar (3+ raqamli) → songa: «bir million besh yuz ming» → «1,500,000»,\n"
            "     «o'ttiz besh» → «35», «uchta» → «3 ta», «o'ninchi vazifa» → «10-vazifa»\n"
            "3) Soat vaqtini aniqlash: agar so'zlovchi «kunduzi/peshindan keyin/kechqurun»\n"
            "   demasa, soat 7-11 ertalab → AM, 12 keyin → PM (24h: 13:00-23:59).\n"
            "   Misol: «soat uchda uchrashuv» (ish kontekstda) → «soat 15:00 da uchrashuv».\n\n"
            "QOIDALAR:\n"
            "- Boshqa so'zlarga tegma. Mazmunni saqla.\n"
            "- Tinish belgilarini o'zgartirma (vergullar, nuqta — o'rni o'rnida).\n"
            "- Glossary so'zi matnda yo'q bo'lsa — uni zo'rlab kiritma.\n"
            "- Faqat tuzatilgan matnni qaytar (izoh, prefiks, qo'shtirnoq yo'q).\n\n"
            f"GLOSSARY:\n"
            f"Ismlar: {names_block}\n"
            f"Atamalar: {glossary_block}\n\n"
            f"INPUT:\n{text}"
        )

        resp = await _anthropic_client.messages.create(
            model=_CORRECTION_MODEL,
            max_tokens=min(len(text) * 2 + 200, 2000),
            messages=[{"role": "user", "content": prompt}],
        )
        corrected = (resp.content[0].text if resp.content else "").strip()
        if not corrected:
            return text
        # Safety: if Claude went off-script (truncated or expanded massively), keep original
        if len(corrected) > len(text) * 3 or len(corrected) < len(text) // 3:
            logger.warning(
                "Transcript correction shape off (orig=%d, corr=%d) — using original",
                len(text), len(corrected),
            )
            return text

        usage = getattr(resp, "usage", None)
        await database.log_llm_call(
            "anthropic", _CORRECTION_MODEL, "voice_transcript_correction",
            None, len(text),
            getattr(usage, "input_tokens", 0) if usage else 0,
            getattr(usage, "output_tokens", 0) if usage else 0,
        )
        if corrected != text:
            logger.info("Transcript fixed: %r -> %r", text[:80], corrected[:80])
        return corrected
    except Exception as e:
        logger.warning("Transcript correction failed (%s: %s) — using original", type(e).__name__, e)
        return text


async def transcribe(audio_bytes: bytes, filename: str = "voice.ogg", language: Optional[str] = "uz") -> Optional[str]:
    """Transcribe audio. Returns transcript string or None on failure.

    Provider chain (each only used if its API key is configured):
      1. Aisha AI       — primary, Uzbek-native, data in UZ.
      2. Muxlisa.uz     — legacy fallback during migration period.
      3. OpenAI Whisper — final fallback with Uzbek primer.
    """
    if not audio_bytes:
        return None
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        logger.warning("Audio rejected: %d bytes > %d MB limit", len(audio_bytes), MAX_AUDIO_BYTES // (1024 * 1024))
        await database.log_llm_call(
            "stt", "preflight", "voice_transcribe",
            None, len(audio_bytes), None, None, error="size_limit",
        )
        return None
    # Skip silent/empty clips before paying for an STT round-trip. A real
    # Telegram voice message is at least a few KB; anything below that is
    # almost certainly an accidental tap or empty recording.
    if len(audio_bytes) < _SILENCE_BYTES_THRESHOLD:
        logger.info("Audio skipped: %d bytes below silence threshold", len(audio_bytes))
        await database.log_llm_call(
            "stt", "preflight", "voice_transcribe",
            None, len(audio_bytes), None, None, error="silence_skip",
        )
        return None

    audio_hash = hashlib.sha256(audio_bytes).hexdigest()[:16]
    lang = language or "uz"

    # ── Attempt 1: Aisha (primary) ──
    if config.AISHA_API_KEY:
        try:
            transcript = await _transcribe_aisha(audio_bytes, filename, lang)
            if transcript is not None:
                await database.log_llm_call(
                    "aisha", "stt-v1", "voice_transcribe",
                    audio_hash, len(audio_bytes), None, len(transcript),
                )
                if transcript:
                    transcript = await _correct_transcript(transcript)
                return transcript or None
        except Exception as e:
            logger.warning("Aisha STT failed (%s: %s) — trying next provider", type(e).__name__, e)
            await database.log_llm_call(
                "aisha", "stt-v1", "voice_transcribe",
                audio_hash, len(audio_bytes), None, None, error=type(e).__name__,
            )

    # ── Attempt 2: Muxlisa (legacy secondary) ──
    if config.MUXLISA_API_KEY:
        try:
            transcript = await _transcribe_muxlisa(audio_bytes, filename, lang)
            if transcript is not None:
                await database.log_llm_call(
                    "muxlisa", "stt-v2", "voice_transcribe",
                    audio_hash, len(audio_bytes), None, len(transcript),
                )
                if transcript:
                    transcript = await _correct_transcript(transcript)
                return transcript or None
        except Exception as e:
            logger.warning("Muxlisa STT failed (%s: %s) — falling back to Whisper", type(e).__name__, e)
            await database.log_llm_call(
                "muxlisa", "stt-v2", "voice_transcribe",
                audio_hash, len(audio_bytes), None, None, error=type(e).__name__,
            )

    # ── Attempt 3: OpenAI Whisper fallback ──
    transcript = await _transcribe_whisper(audio_bytes, filename, audio_hash)
    return transcript


async def _transcribe_aisha(audio_bytes: bytes, filename: str, language: str) -> Optional[str]:
    """Single Aisha STT call (v1 sync endpoint). Raises on network/HTTP error,
    returns text on success.

    v1 is synchronous and ideal for short Telegram voice messages (<60s).
    For long-form audio (>1 min) Aisha recommends v2 (async with polling)."""
    max_attempts = 4
    transient_statuses = {429, 500, 502, 503, 504}

    async with httpx.AsyncClient(timeout=AISHA_TIMEOUT, verify=_CA_BUNDLE) as client:
        files = {"audio": (filename, audio_bytes, "audio/ogg")}
        # language defaults to uz on Aisha; pass explicitly anyway so a future
        # Russian-dominant deployment doesn't silently auto-detect wrong.
        data = {"language": language}
        headers = {"X-Api-Key": config.AISHA_API_KEY}

        for attempt in range(1, max_attempts + 1):
            try:
                resp = await client.post(config.AISHA_STT_URL, files=files, data=data, headers=headers)
                if resp.status_code != 200:
                    if resp.status_code in transient_statuses and attempt < max_attempts:
                        logger.warning(
                            "Aisha transient HTTP %s on attempt %d; retrying",
                            resp.status_code, attempt,
                        )
                        await asyncio.sleep(0.75 * attempt)
                        continue
                    raise httpx.HTTPStatusError(
                        f"Aisha returned {resp.status_code}: {resp.text[:200]}",
                        request=resp.request,
                        response=resp,
                    )

                try:
                    payload = resp.json()
                except Exception:
                    raise ValueError(f"Aisha non-JSON response: {resp.text[:200]}")

                status = (payload.get("status") or "").upper()
                # Treat empty status as success (some endpoints omit it on sync replies).
                if status and status not in ("SUCCESS", "DONE", "OK"):
                    raise RuntimeError(
                        f"Aisha status={status} error={payload.get('error') or payload.get('message') or 'unknown'}"
                    )

                # Aisha v1 returns the result under "transcript". Defensive fallback to
                # "text" in case the v1/v2 schemas diverge in a future update.
                text = (payload.get("transcript") or payload.get("text") or "").strip()
                logger.info("Aisha transcript: %d chars (duration=%.1fs)",
                             len(text), float(payload.get("duration") or 0))
                return text
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                if attempt < max_attempts and isinstance(e, httpx.TransportError):
                    logger.warning(
                        "Aisha transport error on attempt %d: %s; retrying",
                        attempt, type(e).__name__,
                    )
                    await asyncio.sleep(1 * attempt)
                    continue
                raise


async def _transcribe_muxlisa(audio_bytes: bytes, filename: str, language: str) -> Optional[str]:
    """Single Muxlisa call. Raises on network/HTTP error, returns text on success."""
    async with httpx.AsyncClient(timeout=MUXLISA_TIMEOUT, verify=_muxlisa_verify()) as client:
        files = {"audio": (filename, audio_bytes, "audio/ogg")}
        data = {"language": language}
        headers = {"x-api-key": config.MUXLISA_API_KEY}
        resp = await client.post(config.MUXLISA_STT_URL, files=files, data=data, headers=headers)

    if resp.status_code != 200:
        raise httpx.HTTPStatusError(
            f"Muxlisa returned {resp.status_code}: {resp.text[:200]}",
            request=resp.request,
            response=resp,
        )

    try:
        payload = resp.json()
    except Exception:
        raise ValueError(f"Muxlisa non-JSON response: {resp.text[:200]}")

    text = (payload.get("text") or "").strip()
    logger.info("Muxlisa transcript: %d chars", len(text))
    return text


async def _transcribe_whisper(audio_bytes: bytes, filename: str, audio_hash: str) -> Optional[str]:
    """OpenAI Whisper fallback with Uzbek primer."""
    buf = io.BytesIO(audio_bytes)
    buf.name = filename
    try:
        result = await _openai_client.audio.transcriptions.create(
            model=config.WHISPER_MODEL,
            file=buf,
            response_format="text",
            prompt=_UZBEK_PRIMER,
        )
        transcript = (result if isinstance(result, str) else getattr(result, "text", "")).strip()
        await database.log_llm_call(
            "openai", config.WHISPER_MODEL, "voice_transcribe",
            audio_hash, len(audio_bytes), None, len(transcript) if transcript else 0,
        )
        return transcript or None
    except Exception as e:
        logger.exception("Whisper fallback also failed")
        await database.log_llm_call(
            "openai", config.WHISPER_MODEL, "voice_transcribe",
            audio_hash, len(audio_bytes), None, None, error=type(e).__name__,
        )
        return None
