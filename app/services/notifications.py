import logging
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread, Timer
from firebase_admin import messaging

from app.core.config import get_settings
from app.core.supabase import get_supabase_client
from app.services.outfits import create_outfit_suggestion
from app.services.google_calendar import refresh_access_token, sync_google_events
from app.services.timezones import resolve_timezone

logger = logging.getLogger("stylestack.notifications")


class NotificationScheduler:
    def __init__(self) -> None:
        self._stop = Event()
        self._thread: Thread | None = None
        self._completion_lock = Lock()
        self._completion_counts: dict[str, int] = {}
        self._completion_timers: dict[str, Timer] = {}

    def start(self) -> None:
        settings = get_settings()
        if not settings.notification_scheduler_enabled or self._thread:
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="stylestack-notifications", daemon=True)
        self._thread.start()
        logger.info("notification_scheduler_started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        logger.info("notification_scheduler_stopped")

    def _run(self) -> None:
        while not self._stop.wait(get_settings().notification_poll_seconds):
            try:
                self._process_due_users()
            except Exception:
                logger.exception("notification_scheduler_cycle_failed")

    def _process_due_users(self) -> None:
        client = get_supabase_client()
        self._sync_connected_calendars(client)
        profiles = client.table("profiles").select(
            "firebase_uid,city,timezone,notification_time,last_notification_date"
        ).eq("notification_enabled", True).execute().data or []
        for profile in profiles:
            try:
                now = datetime.now(resolve_timezone(profile.get("timezone")))
                configured = str(profile.get("notification_time") or "08:00:00")[:5]
                if now.strftime("%H:%M") != configured:
                    continue
                if str(profile.get("last_notification_date") or "") == now.date().isoformat():
                    self.process_event_reminders(client, profile, now)
                    continue
                try:
                    self.process_daily_outfit(client, profile, now)
                except Exception:
                    logger.exception(
                        "daily_outfit_notification_failed uid=%s", profile["firebase_uid"]
                    )
                self.process_event_reminders(client, profile, now)
            except Exception:
                logger.exception("daily_outfit_notification_failed uid=%s", profile.get("firebase_uid"))

    def _sync_connected_calendars(self, client) -> None:
        if not get_settings().google_calendar_auto_sync_enabled:
            return
        profiles = client.table("profiles").select(
            "firebase_uid,google_calendar_refresh_token,google_calendar_last_synced_at"
        ).eq("google_calendar_connected", True).execute().data or []
        now = datetime.now(timezone.utc)
        for profile in profiles:
            last_value = profile.get("google_calendar_last_synced_at")
            if last_value:
                last_sync = datetime.fromisoformat(str(last_value).replace("Z", "+00:00"))
                interval = max(
                    60, get_settings().google_calendar_sync_interval_seconds
                )
                if now - last_sync < timedelta(seconds=interval):
                    continue
            try:
                access_token = refresh_access_token(profile["google_calendar_refresh_token"])
                imported = sync_google_events(
                    client, profile["firebase_uid"], access_token
                )
                logger.info(
                    "google_calendar_auto_sync_completed uid=%s events=%s",
                    profile["firebase_uid"],
                    imported,
                )
            except Exception:
                logger.exception(
                    "google_calendar_auto_sync_failed uid=%s", profile["firebase_uid"]
                )

    def _deliver(
        self,
        client,
        uid: str,
        notification_type: str,
        title: str,
        body: str,
        data: dict[str, str],
        dedupe_key: str,
        *,
        send_push: bool = True,
    ) -> None:
        client.table("app_notifications").upsert(
            {
                "owner_firebase_uid": uid,
                "type": notification_type,
                "title": title,
                "body": body,
                "data": data,
                "dedupe_key": dedupe_key,
            },
            on_conflict="owner_firebase_uid,dedupe_key",
        ).execute()
        if not send_push:
            logger.info("in_app_notification_created uid=%s type=%s", uid, notification_type)
            return
        tokens = client.table("device_tokens").select("token").eq(
            "owner_firebase_uid", uid
        ).execute().data or []
        if not tokens:
            logger.info("in_app_notification_created uid=%s type=%s", uid, notification_type)
            return
        message = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            data=data,
            tokens=[row["token"] for row in tokens],
        )
        messaging.send_each_for_multicast(message)

    def notify_wardrobe_item_ready(self, uid: str) -> None:
        """Debounce a burst of completed uploads into one useful notification."""
        with self._completion_lock:
            self._completion_counts[uid] = self._completion_counts.get(uid, 0) + 1
            existing = self._completion_timers.pop(uid, None)
            if existing:
                existing.cancel()
            timer = Timer(5, self._flush_wardrobe_ready, args=(uid,))
            timer.daemon = True
            self._completion_timers[uid] = timer
            timer.start()

    def _flush_wardrobe_ready(self, uid: str) -> None:
        with self._completion_lock:
            count = self._completion_counts.pop(uid, 0)
            self._completion_timers.pop(uid, None)
        if count <= 0:
            return
        try:
            client = get_supabase_client()
            profile = (
                client.table("profiles")
                .select("notification_enabled")
                .eq("firebase_uid", uid)
                .limit(1)
                .execute()
            )
            push_enabled = bool(
                profile.data and profile.data[0].get("notification_enabled")
            )
            title = "Your wardrobe item is ready" if count == 1 else f"Your {count} items are ready"
            self._deliver(
                client,
                uid,
                "wardrobe_ready",
                title,
                "AI details are complete. Open your wardrobe to review them.",
                {"type": "wardrobe_ready"},
                f"wardrobe-ready:{uid}:{int(datetime.now(timezone.utc).timestamp() // 5)}",
                send_push=push_enabled,
            )
        except Exception:
            logger.exception("wardrobe_ready_notification_failed uid=%s", uid)

    def process_daily_outfit(self, client, profile: dict, now: datetime) -> str:
        """Create and deliver the daily outfit. Used by both scheduler and simulations."""
        city = profile.get("city")
        if not city:
            raise ValueError("Set your city before generating the daily outfit")
        outfit = create_outfit_suggestion(profile["firebase_uid"], city, "daily")
        self._deliver(
            client,
            profile["firebase_uid"],
            "daily_outfit",
            "Your StyleStack outfit is ready",
            outfit["reasoning"][:180],
            {"type": "daily_outfit", "outfit_id": str(outfit["id"])},
            f"daily:{now.date().isoformat()}",
        )
        client.table("profiles").update(
            {"last_notification_date": now.date().isoformat()}
        ).eq("firebase_uid", profile["firebase_uid"]).execute()
        logger.info("daily_outfit_notification_sent uid=%s", profile["firebase_uid"])
        return str(outfit["id"])

    def schedule_daily_outfit_test(
        self, client, profile: dict, delay_seconds: int = 10
    ) -> None:
        """Run the production daily notification path after a short test delay."""

        def deliver() -> None:
            try:
                now = datetime.now(resolve_timezone(profile.get("timezone")))
                self.process_daily_outfit(client, profile, now)
                logger.info(
                    "daily_outfit_delayed_test_sent uid=%s delay_seconds=%s",
                    profile["firebase_uid"],
                    delay_seconds,
                )
            except Exception:
                logger.exception(
                    "daily_outfit_delayed_test_failed uid=%s",
                    profile.get("firebase_uid"),
                )

        timer = Timer(delay_seconds, deliver)
        timer.daemon = True
        timer.start()

    def process_event_reminders(
        self, client, profile: dict, now: datetime, *, force: bool = False
    ) -> list[str]:
        """Create tomorrow-event reminders. Simulations call this same production path."""
        tomorrow = now.date() + timedelta(days=1)
        query = client.table("calendar_events").select("*").eq(
            "owner_firebase_uid", profile["firebase_uid"]
        )
        if not force:
            query = query.is_("reminder_sent_at", "null")
        events = query.execute().data or []
        processed: list[str] = []
        for event in events:
            event_start = datetime.fromisoformat(str(event["start_at"]).replace("Z", "+00:00"))
            if event_start.astimezone(now.tzinfo).date() != tomorrow:
                continue
            outfit = None
            if profile.get("city"):
                try:
                    occasion = str(event.get("title") or "event").strip()
                    event_type = str(event.get("occasion") or "").strip()
                    if event_type and event_type.lower() != "event":
                        occasion = f"{occasion} - {event_type}"
                    outfit = create_outfit_suggestion(
                        profile["firebase_uid"], profile["city"], occasion[:80]
                    )
                except Exception:
                    logger.exception("event_outfit_generation_failed event_id=%s", event["id"])
            data = {"type": "event_outfit", "event_id": str(event["id"])}
            body = f"Plan your look now for {event['title']} tomorrow."
            updates = {"reminder_sent_at": datetime.now(timezone.utc).isoformat()}
            if outfit:
                data["outfit_id"] = str(outfit["id"])
                updates["outfit_id"] = str(outfit["id"])
                body = f"Your outfit for {event['title']} is ready for tomorrow."
            self._deliver(
                client,
                profile["firebase_uid"],
                "event_outfit",
                f"Tomorrow: {event['title']}",
                body,
                data,
                f"event:{event['id']}",
            )
            client.table("calendar_events").update(updates).eq("id", event["id"]).execute()
            logger.info("event_reminder_sent uid=%s event_id=%s", profile["firebase_uid"], event["id"])
            processed.append(str(event["id"]))
        return processed


notification_scheduler = NotificationScheduler()
