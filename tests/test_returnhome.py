"""Tests for the pure Story #8 return-home core (ETA, lead-time, latch decision)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from custom_components.villa_hvac.supervisor import (
    RETURN_PRECOND,
    RETURN_WAITING,
    ReturnRoom,
    return_decision,
    return_eta,
    return_lead_time,
)

TZ = timezone.utc
HOURS = {"mattino": 8, "pomeriggio": 14, "sera": 19}


def _now(h: int, m: int = 0, d: int = 15) -> datetime:
    return datetime(2026, 7, d, h, m, tzinfo=TZ)


# --- return_eta --------------------------------------------------------------


def test_eta_composes_date_plus_daypart():
    eta = return_eta(date(2026, 7, 16), "sera", HOURS, _now(10))
    assert eta == datetime(2026, 7, 16, 19, 0, tzinfo=TZ)


def test_eta_none_when_incomplete_or_unknown_daypart():
    assert return_eta(None, "sera", HOURS, _now(10)) is None
    assert return_eta(date(2026, 7, 16), None, HOURS, _now(10)) is None
    assert return_eta(date(2026, 7, 16), "notte", HOURS, _now(10)) is None


def test_eta_in_the_past_is_returned_as_is():
    # today morning but it's already afternoon -> a past ETA, caller decides.
    eta = return_eta(date(2026, 7, 15), "mattino", HOURS, _now(14))
    assert eta == datetime(2026, 7, 15, 8, 0, tzinfo=TZ)


# --- return_lead_time --------------------------------------------------------


def _room(temp, target=24.0, a=0.03, b=0.0008, c=0.0, k=1.2) -> ReturnRoom:
    return ReturnRoom(temp=temp, target=target, a=a, b=b, c=c, k=k)


def test_lead_time_easy_room_scales_with_delta():
    # 1.2 °C over target, k≈1.2/h net -> ~1h + 30min margin.
    lead = return_lead_time(
        [_room(25.2)], outdoor=24.0, solar=0.0,
        max_lead=timedelta(hours=6), margin=timedelta(minutes=30),
    )
    assert timedelta(hours=1, minutes=15) < lead < timedelta(hours=1, minutes=45)


def test_lead_time_takes_the_slowest_room():
    rooms = [_room(24.5), _room(27.0)]  # second is much warmer
    lead = return_lead_time(
        rooms, outdoor=24.0, solar=0.0,
        max_lead=timedelta(hours=6), margin=timedelta(minutes=30),
    )
    slow = return_lead_time(
        [_room(27.0)], outdoor=24.0, solar=0.0,
        max_lead=timedelta(hours=6), margin=timedelta(minutes=30),
    )
    assert lead == slow


def test_lead_time_gain_limited_room_clamps_to_max():
    # Peak: huge outdoor gain overwhelms k -> net rate floored -> clamp to max_lead.
    lead = return_lead_time(
        [_room(30.0, a=0.5, k=1.2)], outdoor=40.0, solar=800.0,
        max_lead=timedelta(hours=6), margin=timedelta(minutes=30),
    )
    assert lead == timedelta(hours=6)


def test_lead_time_already_cool_is_min_lead():
    lead = return_lead_time(
        [_room(23.0)], outdoor=24.0, solar=0.0,
        max_lead=timedelta(hours=6), margin=timedelta(minutes=30),
        min_lead=timedelta(minutes=15),
    )
    # no ΔT -> worst 0 -> margin (30) clamped up from min_lead (15) = 30min.
    assert lead == timedelta(minutes=30)


# --- return_decision + latch -------------------------------------------------

ETA = datetime(2026, 7, 15, 19, 0, tzinfo=TZ)
LEAD = timedelta(hours=2)  # window opens 17:00


def _decide(now, latched=False, is_via=True, armed=True, opt_in=True):
    return return_decision(
        is_via=is_via, armed=armed, opt_in=opt_in,
        eta=ETA, lead_time=LEAD, now=now, latched=latched,
    )


def test_decision_inert_when_not_via_or_disarmed_or_optout():
    for kw in ({"is_via": False}, {"armed": False}, {"opt_in": False}):
        decision, latched = _decide(_now(18), latched=True, **kw)
        assert decision is None
        assert latched is False  # latch clears when inert


def test_decision_inert_without_eta():
    decision, latched = return_decision(
        is_via=True, armed=True, opt_in=True,
        eta=None, lead_time=LEAD, now=_now(18), latched=False,
    )
    assert decision is None and latched is False


def test_decision_waiting_before_window():
    decision, latched = _decide(_now(16))  # before 17:00
    assert decision == RETURN_WAITING
    assert latched is False


def test_decision_precond_at_window_open_and_latches():
    decision, latched = _decide(_now(17))  # window opens
    assert decision == RETURN_PRECOND
    assert latched is True


def test_latch_holds_precond_even_if_lead_shrinks():
    # Once latched, an earlier `now` with a shrunk lead still stays PRECOND —
    # this is the anti-chatter guarantee (rooms cooling shortens lead_time).
    decision, latched = return_decision(
        is_via=True, armed=True, opt_in=True,
        eta=ETA, lead_time=timedelta(minutes=30), now=_now(16, 50),
        latched=True,
    )
    assert decision == RETURN_PRECOND and latched is True


def test_decision_holds_precond_past_eta():
    decision, latched = _decide(_now(21), latched=True)  # after ETA, still Via
    assert decision == RETURN_PRECOND and latched is True


# --- AwayReturnController.apply (effective-mode override glue) ----------------

import types  # noqa: E402

from custom_components.villa_hvac import returnhome as rh  # noqa: E402
from custom_components.villa_hvac.const import (  # noqa: E402
    HOUSE_MODE_AWAY,
    HOUSE_MODE_HOME,
    HOUSE_MODE_VACATION,
)
from custom_components.villa_hvac.supervisor import HouseState, ZoneSnapshot  # noqa: E402


def _house(now, *, house_mode=HOUSE_MODE_AWAY, setpoint=24.0, temp=27.0):
    z = ZoneSnapshot(
        zone_id="main_bedroom", name="Padronale", climate="climate.mb",
        emitter="fancoil", temp=temp, enabled=True,
    )
    return HouseState(
        now=now, zones={"main_bedroom": z}, house_mode=house_mode,
        house_setpoint=setpoint, mode_offset=5.0, outdoor_temp=30.0, solar=200.0,
    )


def _patch(monkeypatch, *, opt_in=True, armed=True, rdate=date(2026, 7, 15), daypart="sera"):
    monkeypatch.setattr(rh, "return_precond_enabled", lambda h, e: opt_in)
    monkeypatch.setattr(rh, "return_armed", lambda h, e: armed)
    monkeypatch.setattr(rh, "return_date", lambda h, e: rdate)
    monkeypatch.setattr(rh, "return_daypart", lambda h, e: daypart)


_ENTRY = types.SimpleNamespace(options={})


def test_apply_waiting_overrides_to_vacation(monkeypatch):
    _patch(monkeypatch)
    ctrl = rh.AwayReturnController()
    # ETA 19:00; ΔT=3 over target with net rate ~0.86/h -> window opens ~15:00.
    out = ctrl.apply(_house(_now(10)), None, _ENTRY, commit=True)
    assert out.house_mode == HOUSE_MODE_VACATION
    assert out.mode_offset is None
    assert ctrl.decision == RETURN_WAITING


def test_apply_precond_overrides_to_home(monkeypatch):
    _patch(monkeypatch)
    ctrl = rh.AwayReturnController()
    out = ctrl.apply(_house(_now(17)), None, _ENTRY, commit=True)
    assert out.house_mode == HOUSE_MODE_HOME
    assert out.mode_offset == 0.0
    assert ctrl.decision == RETURN_PRECOND


def test_apply_inert_when_not_via(monkeypatch):
    _patch(monkeypatch)
    ctrl = rh.AwayReturnController()
    src = _house(_now(17), house_mode=HOUSE_MODE_HOME)
    out = ctrl.apply(src, None, _ENTRY, commit=True)
    assert out is src  # unchanged
    assert ctrl.decision is None


def test_apply_inert_when_optout(monkeypatch):
    _patch(monkeypatch, opt_in=False)
    ctrl = rh.AwayReturnController()
    src = _house(_now(17))
    out = ctrl.apply(src, None, _ENTRY, commit=True)
    assert out is src and ctrl.decision is None


def test_apply_latch_survives_when_not_committing(monkeypatch):
    _patch(monkeypatch)
    ctrl = rh.AwayReturnController()
    ctrl.apply(_house(_now(17)), None, _ENTRY, commit=True)  # latches
    # a deploy-dark (commit=False) pass earlier in time still reports precond and
    # must NOT clear the latch.
    out = ctrl.apply(_house(_now(11)), None, _ENTRY, commit=False)
    assert out.house_mode == HOUSE_MODE_HOME
    assert ctrl._latched is True
