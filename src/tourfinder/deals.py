"""Explainable deal rules using observations of an identical offer."""
from datetime import datetime, timedelta, timezone


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def is_available(offer: dict, now: datetime) -> bool:
    try:
        age = now - parse_time(offer["fetched_at"])
        return (timedelta(seconds=-30) <= age <= timedelta(hours=6)
                and offer["date_start"] >= now.date().isoformat()
                and offer["currency"] == "EUR" and offer["price_cents"] > 0
                and str(offer.get("stop_sale") or "").strip().lower() in {"", "0", "false", "no", "n"})
    except (ValueError, TypeError, KeyError):
        return False


def assess(offer: dict, history: list[dict], policy: dict, *,
           now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    if not is_available(offer, now):
        return None
    mode = policy.get("notify_mode", "deal")
    evidence = None
    # Waavo does not yet distinguish rooms reliably in the stored identity.
    if mode in {"deal", "both"} and offer.get("source") != "waavo":
        try:
            rating = float(offer.get("review_rating") or 0) * 5 / float(offer.get("review_scale") or 5)
            quality_ok = (offer.get("review_match_status") == "ok"
                          and rating >= policy.get("min_review_rating", 4)
                          and (offer.get("review_count") or 0) >= policy.get("min_review_count", 20))
        except (ValueError, TypeError, ZeroDivisionError):
            quality_ok = False
        if quality_ok and len(history) >= 3:
            # Ascending (fetched_at, id), limited to the last 14 days.
            price = offer["price_cents"]
            i = len(history) - 1
            while i >= 0 and history[i]["price_cents"] == price and history[i]["currency"] == "EUR":
                i -= 1
            if 0 <= i < len(history) - 1:
                baseline, first_low = history[i], history[i + 1]
                j = i
                while j > 0 and history[j - 1]["price_cents"] == baseline["price_cents"] and history[j - 1]["currency"] == baseline["currency"]:
                    j -= 1
                saving = baseline["price_cents"] - price
                drop_pct = saving * 100 / max(1, baseline["price_cents"])
                sustained = parse_time(baseline["fetched_at"]) - parse_time(history[j]["fetched_at"])
                drop_age = now - parse_time(first_low["fetched_at"])
                if (baseline["currency"] == "EUR" and saving > 0
                        and sustained >= timedelta(hours=6) and i > j
                        and timedelta(0) <= drop_age <= timedelta(hours=72)
                        and saving >= policy.get("min_saving_cents", 10000)
                        and drop_pct >= policy.get("min_drop_pct", 10)):
                    evidence = {"kind": "deal", "baseline_cents": baseline["price_cents"],
                                "saving_cents": saving, "drop_pct": round(drop_pct, 1),
                                "baseline_from": history[j]["fetched_at"],
                                "baseline_till": baseline["fetched_at"],
                                "drop_observed_at": first_low["fetched_at"],
                                "rating_5": round(rating, 2), "review_count": offer["review_count"]}
    if evidence is None and mode in {"budget", "both"}:
        budget = policy.get("budget_max")
        if budget and offer["price_cents"] <= budget * 100:
            evidence = {"kind": "budget", "budget_cents": budget * 100}
    if evidence:
        evidence["observed_at"] = offer["fetched_at"]
    return evidence
