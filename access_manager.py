"""
access_manager.py — Access Code Authorization & Invite Gate Engine for Winz.

Protects Winz Bot from unauthorized public use:
  - Requires public users to enter a valid access code (/access <CODE> or /start <CODE>)
  - Supports single-use, multi-use, or unlimited access codes
  - Allows Admins to generate, list, and revoke access codes directly via Telegram
  - Master Desk Admins (6724880386 & config admins) bypass access code checks automatically
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

log = logging.getLogger(__name__)

_ACCESS_FILE = Path(__file__).parent / "access_codes.json"
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_admin(chat_id: Union[str, int]) -> bool:
    """Check if chat_id belongs to a master desk admin."""
    cid_str = str(chat_id).strip()
    admin_ids = {"6724880386"}
    try:
        from config import cfg
        if cfg.telegram_chat_id:
            admin_ids.add(str(cfg.telegram_chat_id).strip())
    except Exception:
        pass
    extra_admins = os.getenv("ADMIN_CHAT_IDS", "")
    for aid in extra_admins.split(","):
        aid_clean = aid.strip()
        if aid_clean:
            admin_ids.add(aid_clean)
    return cid_str in admin_ids


def _get_default_seed_code() -> str:
    env_code = os.getenv("WINZ_ACCESS_CODE", "").strip().upper()
    return env_code if env_code else "WINZ2026"


def _default_store() -> Dict[str, Any]:
    default_code = _get_default_seed_code()
    return {
        "codes": {
            default_code: {
                "code": default_code,
                "max_uses": 0,  # 0 = unlimited
                "used_count": 0,
                "is_active": True,
                "label": "Master Launch Access Code",
                "created_at": _now(),
                "created_by": "system",
                "redeemed_by": [],
            }
        },
        "authorized_users": {},
        "created_at": _now(),
        "updated_at": _now(),
    }


def _load_data() -> Dict[str, Any]:
    if _ACCESS_FILE.exists():
        try:
            with open(_ACCESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not data.get("codes"):
                    default_store = _default_store()
                    data["codes"] = default_store["codes"]
                return data
        except Exception as exc:
            log.error("Error loading access store: %s — seeding default", exc)
    return _default_store()


def _save_data(data: Dict[str, Any]) -> None:
    data["updated_at"] = _now()
    content = json.dumps(data, indent=2)
    for attempt in range(6):
        tmp = _ACCESS_FILE.with_suffix(f".tmp_{os.getpid()}_{attempt}")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            tmp.replace(_ACCESS_FILE)
            return
        except OSError:
            if tmp.exists():
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
            time.sleep(0.04)

    try:
        with open(_ACCESS_FILE, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as exc:
        log.warning("Access store direct write error: %s", exc)


# ── Core Authorization Gate ───────────────────────────────────────────────────

def is_user_authorized(chat_id: Union[str, int]) -> bool:
    """
    Returns True if user is a Master Desk Admin OR has redeemed a valid access code.
    """
    if is_admin(chat_id):
        return True

    cid = str(chat_id).strip()
    with _lock:
        data = _load_data()
        user_info = data.get("authorized_users", {}).get(cid)
        if user_info and user_info.get("is_authorized", True):
            return True

    try:
        import user_store
        if user_store.is_user_has_access(cid):
            return True
    except Exception:
        pass

    return False


def redeem_access_code(chat_id: Union[str, int], code_str: str) -> Tuple[bool, str]:
    """
    Redeem an access code for a given chat_id.
    Returns (success, message).
    """
    cid = str(chat_id).strip()
    if is_admin(chat_id):
        return True, "Master Desk Administrator: Full access is permanently granted."

    if not code_str:
        return False, "Please provide an access code. Usage: `/access <YOUR_CODE>`"

    clean_code = code_str.strip().upper()

    with _lock:
        data = _load_data()
        users = data.setdefault("authorized_users", {})
        if cid in users and users[cid].get("is_authorized", True):
            return True, "You already have active authorized access to Winz Trading Desk!"

        codes = data.setdefault("codes", {})
        if clean_code not in codes:
            return False, f"Invalid access code `{clean_code}`. Please check the code and try again."

        code_meta = codes[clean_code]
        if not code_meta.get("is_active", True):
            return False, f"Access code `{clean_code}` has been deactivated or revoked."

        max_uses = int(code_meta.get("max_uses", 0))
        used_count = int(code_meta.get("used_count", 0))
        if max_uses > 0 and used_count >= max_uses:
            return False, f"Access code `{clean_code}` has reached its maximum redemption limit ({max_uses}/{max_uses})."

        # Grant access
        code_meta["used_count"] = used_count + 1
        redeemed = code_meta.setdefault("redeemed_by", [])
        if cid not in redeemed:
            redeemed.append(cid)

        users[cid] = {
            "chat_id": cid,
            "code": clean_code,
            "granted_at": _now(),
            "is_authorized": True,
        }
        _save_data(data)

    try:
        import user_store
        user_store.set_user_has_access(cid, True, code=clean_code)
    except Exception as exc:
        log.debug("Failed syncing access to user_store: %s", exc)

    log.info("User %s successfully redeemed access code %s", cid, clean_code)
    return True, f"Access code `{clean_code}` activated successfully! Welcome to Winz."


# ── Admin Code Management ──────────────────────────────────────────────────────

def create_access_code(
    code: Optional[str] = None,
    max_uses: int = 0,
    label: str = "",
    created_by: str = "admin",
) -> Tuple[bool, str, dict]:
    """
    Create a new access code. If code is None or empty, generates a random code.
    max_uses=0 means unlimited redemptions.
    """
    if not code or not code.strip():
        rnd = secrets.token_hex(3).upper()
        clean_code = f"WINZ-{rnd}"
    else:
        clean_code = code.strip().upper()

    with _lock:
        data = _load_data()
        codes = data.setdefault("codes", {})
        if clean_code in codes and codes[clean_code].get("is_active", True):
            return False, f"Access code `{clean_code}` already exists and is active.", codes[clean_code]

        record = {
            "code": clean_code,
            "max_uses": max(0, int(max_uses)),
            "used_count": 0,
            "is_active": True,
            "label": label.strip() or "Standard Invite",
            "created_at": _now(),
            "created_by": str(created_by),
            "redeemed_by": [],
        }
        codes[clean_code] = record
        _save_data(data)

    log.info("Created access code %s (max_uses=%d) by %s", clean_code, max_uses, created_by)
    return True, f"Access code `{clean_code}` created successfully.", record


def revoke_access_code(code_str: str) -> Tuple[bool, str]:
    """Revoke/deactivate an access code."""
    clean_code = code_str.strip().upper()
    with _lock:
        data = _load_data()
        codes = data.get("codes", {})
        if clean_code not in codes:
            return False, f"Access code `{clean_code}` does not exist."
        codes[clean_code]["is_active"] = False
        codes[clean_code]["revoked_at"] = _now()
        _save_data(data)

    log.info("Revoked access code %s", clean_code)
    return True, f"Access code `{clean_code}` has been revoked."


def grant_user_access(chat_id: Union[str, int], label: str = "Admin Direct Grant") -> bool:
    """Directly authorize a user without requiring a code."""
    cid = str(chat_id).strip()
    with _lock:
        data = _load_data()
        users = data.setdefault("authorized_users", {})
        users[cid] = {
            "chat_id": cid,
            "code": "DIRECT_GRANT",
            "granted_at": _now(),
            "is_authorized": True,
            "label": label,
        }
        _save_data(data)

    try:
        import user_store
        user_store.set_user_has_access(cid, True, code="DIRECT_GRANT")
    except Exception:
        pass

    log.info("Admin directly granted access to chat_id %s", cid)
    return True


def revoke_user_access(chat_id: Union[str, int]) -> bool:
    """Revoke authorization from a specific user."""
    cid = str(chat_id).strip()
    with _lock:
        data = _load_data()
        users = data.setdefault("authorized_users", {})
        if cid in users:
            users[cid]["is_authorized"] = False
            users[cid]["revoked_at"] = _now()
        else:
            users[cid] = {
                "chat_id": cid,
                "is_authorized": False,
                "revoked_at": _now(),
            }
        _save_data(data)

    try:
        import user_store
        user_store.set_user_has_access(cid, False)
    except Exception:
        pass

    log.info("Revoked access for chat_id %s", cid)
    return True


def list_access_codes() -> Dict[str, dict]:
    """Return all configured access codes."""
    with _lock:
        return dict(_load_data().get("codes", {}))


def list_authorized_users() -> Dict[str, dict]:
    """Return all authorized users."""
    with _lock:
        return dict(_load_data().get("authorized_users", {}))
