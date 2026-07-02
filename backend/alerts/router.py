"""
Keyword alerting REST API.

Prefixes:
  /api/keyword-groups   — keyword grouping CRUD
  /api/keywords         — keyword CRUD (word/phrase, group, type, score)
  /api/channels         — notification channel CRUD (Telegram now, more later)
  /api/notification-groups — channel grouping CRUD
  /api/alert-rules      — alert rule CRUD (keywords + blacklist + threshold ...)
  /api/alerts           — fired alerts (list, ack, verdict, resend, notes)
  /api/alert-stats      — aggregate statistics for dashboards/charts

All config-mutating endpoints are admin-only; read endpoints require a logged-in
user. This mirrors the existing settings-page access model.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.dependencies import get_current_user, require_admin
from backend.models.user import User
from backend.models.keyword import Keyword
from backend.models.keyword_group import KeywordGroup
from backend.models.notification_channel import (
    NotificationChannel, NotificationGroup, APP_TYPES,
)
from backend.models.alert_rule import (
    AlertRule, SEVERITY_LEVELS, KEYWORD_TYPES,
    alert_rule_keywords, alert_rule_blacklist,
    alert_rule_channels, alert_rule_notification_groups,
)
from backend.models.alert_event import (
    AlertEvent, AlertDelivery, AlertJob,
    DELIVERY_PENDING, DELIVERY_SENT, DELIVERY_FAILED,
    VERDICT_UNCONFIRMED, VERDICT_CONFIRMED, VERDICT_FALSE_ALARM,
)
from backend.database import get_db

keyword_groups_router = APIRouter(prefix="/api/keyword-groups", tags=["Keyword Groups"])
keywords_router = APIRouter(prefix="/api/keywords", tags=["Keywords"])
channels_router = APIRouter(prefix="/api/channels", tags=["Notification Channels"])
notif_groups_router = APIRouter(prefix="/api/notification-groups", tags=["Notification Groups"])
alert_rules_router = APIRouter(prefix="/api/alert-rules", tags=["Alert Rules"])
alerts_router = APIRouter(prefix="/api/alerts", tags=["Alerts"])
alert_stats_router = APIRouter(prefix="/api/alert-stats", tags=["Alert Stats"])

VALID_APP_TYPES = set(APP_TYPES.keys())

# =====================================================================
# Keyword Groups
# =====================================================================

class KeywordGroupIn(BaseModel):
    name: str
    description: Optional[str] = None
    color: Optional[str] = None


@keyword_groups_router.get("")
async def list_keyword_groups(current_user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(KeywordGroup).order_by(KeywordGroup.name))).scalars().all()
    return {"items": [g.to_dict() for g in rows]}


@keyword_groups_router.post("", dependencies=[Depends(require_admin)])
async def create_keyword_group(req: KeywordGroupIn, db: AsyncSession = Depends(get_db)):
    g = KeywordGroup(name=req.name, description=req.description, color=req.color)
    db.add(g)
    await db.commit()
    await db.refresh(g)
    return g.to_dict()


@keyword_groups_router.put("/{group_id}", dependencies=[Depends(require_admin)])
async def update_keyword_group(group_id: str, req: KeywordGroupIn,
                               db: AsyncSession = Depends(get_db)):
    g = await db.get(KeywordGroup, group_id)
    if not g:
        raise HTTPException(404, "Keyword group not found")
    g.name = req.name
    g.description = req.description
    g.color = req.color
    await db.commit()
    await db.refresh(g)
    return g.to_dict()


@keyword_groups_router.delete("/{group_id}", dependencies=[Depends(require_admin)])
async def delete_keyword_group(group_id: str, db: AsyncSession = Depends(get_db)):
    g = await db.get(KeywordGroup, group_id)
    if not g:
        raise HTTPException(404, "Keyword group not found")
    # Detach keywords (group_id is ON DELETE SET NULL); SQLAlchemy ORM will not
    # cascade-set, so we do it explicitly.
    res = await db.execute(select(Keyword).where(Keyword.group_id == group_id))
    for kw in res.scalars().all():
        kw.group_id = None
    await db.delete(g)
    await db.commit()
    return {"success": True}


# =====================================================================
# Keywords
# =====================================================================

class KeywordIn(BaseModel):
    keyword: str
    description: Optional[str] = None
    is_active: Optional[bool] = True
    is_case_sensitive: Optional[bool] = False
    group_id: Optional[str] = None
    type: Optional[str] = "other"
    score: Optional[int] = 1


@keywords_router.get("")
async def list_keywords(current_user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Keyword).order_by(Keyword.keyword))).scalars().all()
    return {"items": [k.to_dict() for k in rows]}


@keywords_router.post("", dependencies=[Depends(require_admin)])
async def create_keyword(req: KeywordIn, current_user: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)):
    if req.type and req.type not in KEYWORD_TYPES:
        raise HTTPException(400, f"type must be one of {KEYWORD_TYPES}")
    k = Keyword(
        keyword=req.keyword,
        description=req.description,
        is_active=req.is_active if req.is_active is not None else True,
        is_case_sensitive=req.is_case_sensitive if req.is_case_sensitive is not None else False,
        group_id=req.group_id,
        type=req.type or "other",
        score=req.score if req.score is not None else 1,
        created_by=current_user.id,
    )
    db.add(k)
    await db.commit()
    await db.refresh(k)
    return k.to_dict()


@keywords_router.put("/{keyword_id}", dependencies=[Depends(require_admin)])
async def update_keyword(keyword_id: str, req: KeywordIn, db: AsyncSession = Depends(get_db)):
    k = await db.get(Keyword, keyword_id)
    if not k:
        raise HTTPException(404, "Keyword not found")
    if req.type and req.type not in KEYWORD_TYPES:
        raise HTTPException(400, f"type must be one of {KEYWORD_TYPES}")
    k.keyword = req.keyword
    k.description = req.description
    k.is_active = req.is_active if req.is_active is not None else True
    k.is_case_sensitive = req.is_case_sensitive if req.is_case_sensitive is not None else False
    k.group_id = req.group_id
    k.type = req.type or "other"
    k.score = req.score if req.score is not None else 1
    await db.commit()
    await db.refresh(k)
    return k.to_dict()


@keywords_router.delete("/{keyword_id}", dependencies=[Depends(require_admin)])
async def delete_keyword(keyword_id: str, db: AsyncSession = Depends(get_db)):
    k = await db.get(Keyword, keyword_id)
    if not k:
        raise HTTPException(404, "Keyword not found")
    await db.delete(k)
    await db.commit()
    return {"success": True}
# =====================================================================
# Notification Channels (Telegram now; more app types later)
# =====================================================================

class ChannelIn(BaseModel):
    app_type: str
    name: str
    target: str
    config: Optional[dict] = None
    is_active: Optional[bool] = True


@channels_router.get("")
async def list_channels(current_user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(NotificationChannel).order_by(NotificationChannel.name))).scalars().all()
    return {"items": [c.to_dict() for c in rows], "app_types": APP_TYPES}


@channels_router.post("", dependencies=[Depends(require_admin)])
async def create_channel(req: ChannelIn, db: AsyncSession = Depends(get_db)):
    if req.app_type not in VALID_APP_TYPES:
        raise HTTPException(400, f"app_type must be one of {sorted(VALID_APP_TYPES)}")
    c = NotificationChannel(
        app_type=req.app_type,
        name=req.name,
        target=req.target,
        is_active=req.is_active if req.is_active is not None else True,
    )
    c.set_config(req.config or {})
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c.to_dict()


@channels_router.put("/{channel_id}", dependencies=[Depends(require_admin)])
async def update_channel(channel_id: str, req: ChannelIn, db: AsyncSession = Depends(get_db)):
    c = await db.get(NotificationChannel, channel_id)
    if not c:
        raise HTTPException(404, "Channel not found")
    if req.app_type not in VALID_APP_TYPES:
        raise HTTPException(400, f"app_type must be one of {sorted(VALID_APP_TYPES)}")
    c.app_type = req.app_type
    c.name = req.name
    c.target = req.target
    c.is_active = req.is_active if req.is_active is not None else True
    # Preserve existing secret if the caller sent a masked/empty placeholder.
    cfg = req.config or {}
    merged = c.get_config()
    for k, v in cfg.items():
        if v in (None, "", "***") or "..." in str(v):
            continue  # keep existing secret
        merged[k] = v
    c.set_config(merged)
    await db.commit()
    await db.refresh(c)
    return c.to_dict()


@channels_router.delete("/{channel_id}", dependencies=[Depends(require_admin)])
async def delete_channel(channel_id: str, db: AsyncSession = Depends(get_db)):
    c = await db.get(NotificationChannel, channel_id)
    if not c:
        raise HTTPException(404, "Channel not found")
    await db.delete(c)
    await db.commit()
    return {"success": True}


@channels_router.post("/{channel_id}/test", dependencies=[Depends(require_admin)])
async def test_channel(channel_id: str, db: AsyncSession = Depends(get_db)):
    """Send a connectivity test message to a channel."""
    c = await db.get(NotificationChannel, channel_id)
    if not c:
        raise HTTPException(404, "Channel not found")
    from backend.alerts.providers import get_provider
    provider = get_provider(c.app_type)
    if not provider:
        raise HTTPException(400, f"No provider for app_type {c.app_type}")
    result = await provider.send_test(c)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "Test failed")
    return {"success": True}


# =====================================================================
# Notification Groups
# =====================================================================

class NotifGroupIn(BaseModel):
    name: str
    description: Optional[str] = None
    channel_ids: Optional[list[str]] = None


async def _set_group_channels(db: AsyncSession, group: NotificationGroup, channel_ids: list[str]):
    # See _set_rule_keywords: refresh the collection first to avoid a sync
    # lazy-load (MissingGreenlet) when assigning the M2M attribute.
    await db.refresh(group, attribute_names=["channels"])
    res = await db.execute(select(NotificationChannel).where(NotificationChannel.id.in_(channel_ids or [])))
    group.channels[:] = res.scalars().all()


@notif_groups_router.get("")
async def list_notif_groups(current_user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(NotificationGroup).order_by(NotificationGroup.name))).scalars().all()
    return {"items": [g.to_dict() for g in rows]}


@notif_groups_router.post("", dependencies=[Depends(require_admin)])
async def create_notif_group(req: NotifGroupIn, db: AsyncSession = Depends(get_db)):
    g = NotificationGroup(name=req.name, description=req.description)
    db.add(g)
    await db.flush()
    if req.channel_ids:
        await _set_group_channels(db, g, req.channel_ids)
    await db.commit()
    return await _fetch_notif_group_for_response(db, g.id)


@notif_groups_router.put("/{group_id}", dependencies=[Depends(require_admin)])
async def update_notif_group(group_id: str, req: NotifGroupIn, db: AsyncSession = Depends(get_db)):
    g = await db.get(NotificationGroup, group_id)
    if not g:
        raise HTTPException(404, "Notification group not found")
    g.name = req.name
    g.description = req.description
    await _set_group_channels(db, g, req.channel_ids or [])
    await db.commit()
    return await _fetch_notif_group_for_response(db, g.id)


@notif_groups_router.delete("/{group_id}", dependencies=[Depends(require_admin)])
async def delete_notif_group(group_id: str, db: AsyncSession = Depends(get_db)):
    g = await db.get(NotificationGroup, group_id)
    if not g:
        raise HTTPException(404, "Notification group not found")
    await db.delete(g)
    await db.commit()
    return {"success": True}
# =====================================================================
# Alert Rules
# =====================================================================

class AlertRuleIn(BaseModel):
    name: str
    description: Optional[str] = None
    score_threshold: Optional[int] = 1
    severity: Optional[str] = "warning"
    cooldown_seconds: Optional[int] = 300
    active_time_start: Optional[str] = None
    active_time_end: Optional[str] = None
    timezone: Optional[str] = "Asia/Bangkok"
    is_enabled: Optional[bool] = True
    keyword_ids: Optional[list[str]] = None
    blacklist_ids: Optional[list[str]] = None
    channel_ids: Optional[list[str]] = None
    notification_group_ids: Optional[list[str]] = None


async def _set_rule_keywords(db: AsyncSession, rule: AlertRule, req: AlertRuleIn):
    # In async SQLAlchemy, assigning to an M2M relationship attribute triggers a
    # lazy-load of the existing collection (to diff against), which raises
    # MissingGreenlet (no async context). We force an eager refresh of each
    # relationship first so its collection is materialised, then swap contents in
    # place via slice assignment (sync-safe).
    rels = []
    if req.keyword_ids is not None: rels.append("keywords")
    if req.blacklist_ids is not None: rels.append("blacklist")
    if req.channel_ids is not None: rels.append("channels")
    if req.notification_group_ids is not None: rels.append("notification_groups")
    if rels:
        await db.refresh(rule, attribute_names=rels)

    async def _replace(attr, model, ids):
        res = await db.execute(select(model).where(model.id.in_(ids or [])))
        getattr(rule, attr)[:] = res.scalars().all()

    if req.keyword_ids is not None:
        await _replace("keywords", Keyword, req.keyword_ids)
    if req.blacklist_ids is not None:
        await _replace("blacklist", Keyword, req.blacklist_ids)
    if req.channel_ids is not None:
        await _replace("channels", NotificationChannel, req.channel_ids)
    if req.notification_group_ids is not None:
        await _replace("notification_groups", NotificationGroup, req.notification_group_ids)


async def _fetch_rule_for_response(db: AsyncSession, rule_id: str) -> dict:
    """Re-fetch a rule with all M2M relationships eagerly loaded.

    After a create/update we must NOT call rule.to_dict() on the in-session
    object — accessing M2M relationships on it triggers a sync lazy-load
    (MissingGreenlet) because SQLAlchemy wants to sync the association table.
    A fresh query with selectinload materialises everything cleanly.
    """
    res = await db.execute(
        select(AlertRule)
        .options(
            selectinload(AlertRule.keywords),
            selectinload(AlertRule.blacklist),
            selectinload(AlertRule.channels),
            selectinload(AlertRule.notification_groups),
            selectinload(AlertRule.events),
        )
        .where(AlertRule.id == rule_id)
    )
    r = res.scalar_one_or_none()
    return r.to_dict() if r else None


async def _fetch_event_for_response(db: AsyncSession, event_id: str) -> dict:
    """Re-fetch an alert event with deliveries/rule/user rels loaded (async-safe)."""
    res = await db.execute(
        select(AlertEvent)
        .options(
            selectinload(AlertEvent.deliveries).selectinload(AlertDelivery.channel),
            selectinload(AlertEvent.rule),
            selectinload(AlertEvent.acknowledged_by),
            selectinload(AlertEvent.verdict_by),
        )
        .where(AlertEvent.id == event_id)
    )
    e = res.scalar_one_or_none()
    return e.to_dict() if e else None


async def _fetch_notif_group_for_response(db: AsyncSession, group_id: str) -> dict:
    """Re-fetch a notification group with its channels loaded (see above)."""
    res = await db.execute(
        select(NotificationGroup)
        .options(selectinload(NotificationGroup.channels))
        .where(NotificationGroup.id == group_id)
    )
    g = res.scalar_one_or_none()
    return g.to_dict(include_channels=True) if g else None


@alert_rules_router.get("")
async def list_alert_rules(current_user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AlertRule).order_by(AlertRule.name))).scalars().all()
    return {"items": [r.to_dict() for r in rows]}


@alert_rules_router.post("", dependencies=[Depends(require_admin)])
async def create_alert_rule(req: AlertRuleIn, db: AsyncSession = Depends(get_db)):
    if req.severity and req.severity not in SEVERITY_LEVELS:
        raise HTTPException(400, f"severity must be one of {SEVERITY_LEVELS}")
    r = AlertRule(
        name=req.name,
        description=req.description,
        score_threshold=req.score_threshold if req.score_threshold is not None else 1,
        severity=req.severity or "warning",
        cooldown_seconds=req.cooldown_seconds if req.cooldown_seconds is not None else 300,
        active_time_start=req.active_time_start,
        active_time_end=req.active_time_end,
        timezone=req.timezone or "Asia/Bangkok",
        is_enabled=req.is_enabled if req.is_enabled is not None else True,
    )
    db.add(r)
    await db.flush()
    await _set_rule_keywords(db, r, req)
    await db.commit()
    # Serialize via a fresh eager-loaded query (see _fetch_rule_for_response).
    return await _fetch_rule_for_response(db, r.id)


@alert_rules_router.put("/{rule_id}", dependencies=[Depends(require_admin)])
async def update_alert_rule(rule_id: str, req: AlertRuleIn, db: AsyncSession = Depends(get_db)):
    r = await db.get(AlertRule, rule_id)
    if not r:
        raise HTTPException(404, "Alert rule not found")
    if req.severity and req.severity not in SEVERITY_LEVELS:
        raise HTTPException(400, f"severity must be one of {SEVERITY_LEVELS}")
    r.name = req.name
    r.description = req.description
    r.score_threshold = req.score_threshold if req.score_threshold is not None else 1
    r.severity = req.severity or "warning"
    r.cooldown_seconds = req.cooldown_seconds if req.cooldown_seconds is not None else 300
    r.active_time_start = req.active_time_start
    r.active_time_end = req.active_time_end
    r.timezone = req.timezone or "Asia/Bangkok"
    r.is_enabled = req.is_enabled if req.is_enabled is not None else True
    await _set_rule_keywords(db, r, req)
    await db.commit()
    return await _fetch_rule_for_response(db, r.id)


@alert_rules_router.delete("/{rule_id}", dependencies=[Depends(require_admin)])
async def delete_alert_rule(rule_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.get(AlertRule, rule_id)
    if not r:
        raise HTTPException(404, "Alert rule not found")
    await db.delete(r)
    await db.commit()
    return {"success": True}


@alert_rules_router.post("/{rule_id}/test", dependencies=[Depends(require_admin)])
async def test_alert_rule(rule_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    """Dry-run a rule against sample text; reports whether it would fire and why."""
    r = await db.get(AlertRule, rule_id)
    if not r:
        raise HTTPException(404, "Alert rule not found")
    from backend.alerts.engine import evaluate_rule
    transcript = (body or {}).get("text", "")
    mr = evaluate_rule(r, transcript)
    return {
        "fired": mr.fired,
        "total_score": mr.total_score,
        "matched": mr.matched,
        "reason": mr.reason,
    }
# =====================================================================
# Alerts (fired events) — list, triage, resend
# =====================================================================

class BulkDeleteIn(BaseModel):
    ids: list[str]


@alerts_router.post("/bulk-delete", dependencies=[Depends(require_admin)])
async def bulk_delete_alerts(req: BulkDeleteIn, db: AsyncSession = Depends(get_db)):
    """Bulk delete alert events (admin only). Must be before /{event_id}."""
    if not req.ids:
        raise HTTPException(400, "ids must not be empty")
    res = await db.execute(select(AlertEvent).where(AlertEvent.id.in_(req.ids)))
    events = res.scalars().all()
    deleted_ids = [e.id for e in events]
    found_set = set(deleted_ids)
    not_found = [i for i in req.ids if i not in found_set]
    for ev in events:
        await db.delete(ev)
    await db.commit()
    return {"success": True, "deleted_count": len(deleted_ids), "deleted_ids": deleted_ids, "not_found_ids": not_found}


@alerts_router.get("")
async def list_alerts(limit: int = 50, offset: int = 0,
                      severity: Optional[str] = None,
                      verdict: Optional[str] = None,
                      device_id: Optional[str] = None,
                      current_user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    """List fired alert events (newest first), with optional filters.

    device_id filters by the recording's SDR device. Since AlertEvent stores
    only a device_name snapshot (not a FK), we resolve the device name first
    and match on that.
    """
    q = select(AlertEvent)
    if severity:
        q = q.where(AlertEvent.severity == severity)
    if verdict:
        q = q.where(AlertEvent.verdict == verdict)
    if device_id:
        from backend.models.sdr_device import SdrDevice
        dev = await db.get(SdrDevice, device_id)
        if dev:
            q = q.where(AlertEvent.device_name == dev.name)
        else:
            # Unknown device id → return empty set.
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
    q = q.options(
        selectinload(AlertEvent.deliveries).selectinload(AlertDelivery.channel),
        selectinload(AlertEvent.rule),
    ).order_by(AlertEvent.created_at.desc()).limit(limit).offset(offset)
    items = (await db.execute(q)).scalars().all()

    count_q = select(func.count(AlertEvent.id))
    if severity:
        count_q = count_q.where(AlertEvent.severity == severity)
    if verdict:
        count_q = count_q.where(AlertEvent.verdict == verdict)
    if device_id:
        from backend.models.sdr_device import SdrDevice
        dev = await db.get(SdrDevice, device_id)
        if dev:
            count_q = count_q.where(AlertEvent.device_name == dev.name)
        else:
            total = 0
    else:
        total = (await db.execute(count_q)).scalar() or 0

    return {"items": [e.to_dict() for e in items], "total": total, "limit": limit, "offset": offset}


@alerts_router.get("/{event_id}")
async def get_alert(event_id: str, current_user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    d = await _fetch_event_for_response(db, event_id)
    if not d:
        raise HTTPException(404, "Alert event not found")
    return d


@alerts_router.delete("/{event_id}", dependencies=[Depends(require_admin)])
@alerts_router.post("/{event_id}/delete", dependencies=[Depends(require_admin)])
async def delete_alert(event_id: str, db: AsyncSession = Depends(get_db)):
    """Delete an alert event (admin only).

    Cascade-deletes all associated AlertDelivery rows automatically
    (cascade="all, delete-orphan"). Does NOT delete the recording.
    """
    result = await db.execute(select(AlertEvent).where(AlertEvent.id == event_id))
    ev = result.scalar_one_or_none()
    if not ev:
        raise HTTPException(404, "Alert event not found")
    await db.delete(ev)
    await db.commit()
    return {"success": True, "id": event_id}


class AckIn(BaseModel):
    acknowledged: bool = True


@alerts_router.post("/{event_id}/ack", dependencies=[Depends(require_admin)])
async def ack_alert(event_id: str, req: AckIn, current_user: User = Depends(require_admin),
                    db: AsyncSession = Depends(get_db)):
    """Mark an alert as acknowledged (ผู้รับทราบ) by the current user."""
    from datetime import datetime, timezone
    e = await db.get(AlertEvent, event_id)
    if not e:
        raise HTTPException(404, "Alert event not found")
    if req.acknowledged:
        e.acknowledged_by_user_id = current_user.id
        e.acknowledged_at = datetime.now(timezone.utc)
    else:
        e.acknowledged_by_user_id = None
        e.acknowledged_at = None
    await db.commit()
    return await _fetch_event_for_response(db, event_id)


class VerdictIn(BaseModel):
    verdict: str  # confirmed | false_alarm | unconfirmed


@alerts_router.post("/{event_id}/verdict", dependencies=[Depends(require_admin)])
async def verdict_alert(event_id: str, req: VerdictIn,
                        current_user: User = Depends(require_admin),
                        db: AsyncSession = Depends(get_db)):
    """Set the human verdict (เหตุจริง / false alarm) for an alert."""
    from datetime import datetime, timezone
    if req.verdict not in (VERDICT_UNCONFIRMED, VERDICT_CONFIRMED, VERDICT_FALSE_ALARM):
        raise HTTPException(400, "verdict must be confirmed | false_alarm | unconfirmed")
    e = await db.get(AlertEvent, event_id)
    if not e:
        raise HTTPException(404, "Alert event not found")
    e.verdict = req.verdict
    if req.verdict == VERDICT_UNCONFIRMED:
        e.verdict_by_user_id = None
        e.verdict_at = None
    else:
        e.verdict_by_user_id = current_user.id
        e.verdict_at = datetime.now(timezone.utc)
    await db.commit()
    return await _fetch_event_for_response(db, event_id)


class NotesIn(BaseModel):
    notes: Optional[str] = None


@alerts_router.post("/{event_id}/notes", dependencies=[Depends(require_admin)])
async def notes_alert(event_id: str, req: NotesIn, db: AsyncSession = Depends(get_db)):
    """Update the free-form notes (หมายเหตุ) for an alert."""
    e = await db.get(AlertEvent, event_id)
    if not e:
        raise HTTPException(404, "Alert event not found")
    e.notes = req.notes
    await db.commit()
    return await _fetch_event_for_response(db, event_id)
class ResendIn(BaseModel):
    channel_ids: Optional[list[str]] = None  # if None, resend to all original channels


@alerts_router.post("/{event_id}/resend", dependencies=[Depends(require_admin)])
async def resend_alert(event_id: str, req: ResendIn,
                       current_user: User = Depends(require_admin),
                       db: AsyncSession = Depends(get_db)):
    """Re-send an alert to the chosen channels (creates new pending deliveries).

    The AlertWorker picks up the new pending deliveries on its next tick.
    """
    e = await db.get(AlertEvent, event_id)
    if not e:
        raise HTTPException(404, "Alert event not found")

    target_ids = req.channel_ids
    if target_ids is None:
        # Resend to all channels that were originally targeted by this event.
        target_ids = [d.channel_id for d in (e.deliveries or [])]
    if not target_ids:
        raise HTTPException(400, "No channels to resend to")

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    res = await db.execute(select(NotificationChannel).where(NotificationChannel.id.in_(target_ids)))
    for ch in res.scalars().all():
        db.add(AlertDelivery(
            alert_event_id=event_id,
            channel_id=ch.id,
            status=DELIVERY_PENDING,
            created_at=now,
        ))
    await db.commit()
    return {"success": True, "queued": len(target_ids), "event": await _fetch_event_for_response(db, event_id)}


# =====================================================================
# Alert Stats (for dashboards / charts)
# =====================================================================

@alert_stats_router.get("")
async def alert_stats(current_user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    """Aggregate alerting statistics for dashboards.

    Returns counts by severity, by verdict, by delivery status, by app type,
    and the most-frequently matched keywords.
    """
    sev_rows = (await db.execute(
        select(AlertEvent.severity, func.count(AlertEvent.id))
        .group_by(AlertEvent.severity)
    )).all()

    verdict_rows = (await db.execute(
        select(AlertEvent.verdict, func.count(AlertEvent.id))
        .group_by(AlertEvent.verdict)
    )).all()

    del_rows = (await db.execute(
        select(AlertDelivery.status, func.count(AlertDelivery.id))
        .group_by(AlertDelivery.status)
    )).all()

    app_rows = (await db.execute(
        select(NotificationChannel.app_type, func.count(AlertDelivery.id))
        .select_from(AlertDelivery)
        .join(NotificationChannel, AlertDelivery.channel_id == NotificationChannel.id)
        .where(AlertDelivery.status == DELIVERY_SENT)
        .group_by(NotificationChannel.app_type)
    )).all()

    # Most-matched keywords: scan the JSON matched_keywords blob of each event.
    # SQLite JSON is stored as TEXT, so we decode in Python (fine for stats scale).
    events = (await db.execute(select(AlertEvent.matched_keywords))).all()
    kw_counts: dict[str, int] = {}
    for (blob,) in events:
        if not blob:
            continue
        try:
            import json
            for m in json.loads(blob):
                kw = m.get("keyword")
                if kw:
                    kw_counts[kw] = kw_counts.get(kw, 0) + 1
        except Exception:
            continue
    top_keywords = sorted(kw_counts.items(), key=lambda x: x[1], reverse=True)[:20]

    job_rows = (await db.execute(
        select(AlertJob.status, func.count(AlertJob.id)).group_by(AlertJob.status)
    )).all()

    # Alerts by SDR device (matched on the device_name snapshot). NULLs are
    # grouped under "ไม่ระบุ" so the chart always has a label.
    dev_rows = (await db.execute(
        select(
            func.coalesce(AlertEvent.device_name, "ไม่ระบุ").label("device_name"),
            func.count(AlertEvent.id),
        )
        .group_by("device_name")
        .order_by(func.count(AlertEvent.id).desc())
    )).all()

    return {
        "by_severity": [{"severity": s, "count": c} for s, c in sev_rows],
        "by_verdict": [{"verdict": v, "count": c} for v, c in verdict_rows],
        "by_delivery_status": [{"status": s, "count": c} for s, c in del_rows],
        "by_app_type": [{"app_type": a, "count": c} for a, c in app_rows],
        "top_keywords": [{"keyword": k, "count": c} for k, c in top_keywords],
        "job_queue": [{"status": s, "count": c} for s, c in job_rows],
        "by_device": [{"device_name": d, "count": c} for d, c in dev_rows],
    }


# =====================================================================
# Meta (enum options for the frontend)
# =====================================================================

@alert_rules_router.get("/meta/options")
async def rule_options(current_user: User = Depends(get_current_user)):
    """Return valid enum options for building forms on the frontend."""
    return {
        "severities": list(SEVERITY_LEVELS),
        "keyword_types": list(KEYWORD_TYPES),
        "verdicts": [VERDICT_UNCONFIRMED, VERDICT_CONFIRMED, VERDICT_FALSE_ALARM],
        "delivery_statuses": [DELIVERY_PENDING, DELIVERY_SENT, DELIVERY_FAILED],
        "app_types": APP_TYPES,
    }



# =====================================================================
# Time-series stats (for charts: daily/monthly trends)
# =====================================================================

@alert_stats_router.get("/timeseries")
async def alert_timeseries(
    days: int = 30,
    bucket: str = "day",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Time-series alert counts grouped by day or month.

    Args:
        days: how many days back to include (1-365). Ignored for month bucket
              which returns the last 12 months.
        bucket: "day" (default) or "month".

    Returns:
        {
          "buckets": [{"period": "2026-06-01", "count": 5}, ...],
          "total": 123,
          "bucket_type": "day"|"month",
        }
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import text as sql_text

    bucket = (bucket or "day").lower()
    if bucket not in ("day", "month"):
        bucket = "day"

    if bucket == "month":
        # Last 12 months, grouped by YYYY-MM.
        fmt = "%Y-%m"
        sql = sql_text("""
            SELECT strftime('%Y-%m', datetime(created_at/1000, 'unixepoch')) AS period,
                   COUNT(*) AS cnt
            FROM alert_events
            WHERE created_at >= :since
            GROUP BY period
            ORDER BY period ASC
        """)
        # SQLite stores DateTime as ISO text via aiosqlite; the /1000 unixepoch
        # trick is not applicable. Use a simpler substring on the ISO string.
        sql = sql_text("""
            SELECT substr(created_at, 1, 7) AS period, COUNT(*) AS cnt
            FROM alert_events
            WHERE created_at >= :since
            GROUP BY period
            ORDER BY period ASC
        """)
        since = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    else:
        # Daily, last N days.
        days = max(1, min(int(days or 30), 365))
        sql = sql_text("""
            SELECT substr(created_at, 1, 10) AS period, COUNT(*) AS cnt
            FROM alert_events
            WHERE created_at >= :since
            GROUP BY period
            ORDER BY period ASC
        """)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    res = await db.execute(sql, {"since": since})
    rows = res.all()

    # Fill missing buckets with zero so the chart x-axis is continuous.
    filled = []
    if bucket == "month":
        now = datetime.now(timezone.utc)
        cur = (now.replace(day=1) - timedelta(days=365)).replace(day=1)
        end = now.replace(day=1)
        counts = {p: c for p, c in rows}
        while cur <= end:
            key = cur.strftime("%Y-%m")
            filled.append({"period": key, "count": int(counts.get(key, 0))})
            # next month
            if cur.month == 12:
                cur = cur.replace(year=cur.year + 1, month=1)
            else:
                cur = cur.replace(month=cur.month + 1)
    else:
        now = datetime.now(timezone.utc)
        cur = (now - timedelta(days=days - 1)).date()
        end = now.date()
        counts = {p: c for p, c in rows}
        from datetime import date as date_cls, timedelta as td
        d = cur
        while d <= end:
            key = d.isoformat()
            filled.append({"period": key, "count": int(counts.get(key, 0))})
            d = d + td(days=1)

    total = sum(r[1] for r in rows)
    return {"buckets": filled, "total": int(total), "bucket_type": bucket}
