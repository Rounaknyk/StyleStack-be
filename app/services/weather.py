import httpx

from app.core.config import get_settings
from app.models.outfit import WeatherResponse


def get_current_weather(city: str) -> WeatherResponse:
    settings = get_settings()
    if not settings.openweather_api_key:
        raise RuntimeError("OPENWEATHER_API_KEY is not configured")
    response = httpx.get(
        f"{settings.openweather_base_url}/weather",
        params={
            "q": city,
            "appid": settings.openweather_api_key,
            "units": "metric",
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    weather = payload.get("weather") or [{}]
    return WeatherResponse(
        city=payload.get("name") or city,
        temperature_c=payload["main"]["temp"],
        feels_like_c=payload["main"]["feels_like"],
        condition=weather[0].get("main", "Unknown"),
        description=weather[0].get("description", "Unknown conditions"),
    )
